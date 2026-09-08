import html
import json
import logging
import sqlite3
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests


TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"

FILTER_LABELS = {
    "notify_all": "Все логи",
    "notify_errors": "Ошибки",
    "notify_updated": "Замена торрента",
    "notify_added": "Добавление в qBittorrent",
    "notify_completed": "Скачивание завершено 100%",
}


class TelegramNotifier:
    def __init__(self, db_path, timezone_name="Europe/Moscow"):
        self.db_path = db_path
        self.timezone_name = timezone_name
        self.offset = 0
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="telegram-bot")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def _get_bot_settings(self):
        db = self._connect()
        try:
            row = db.execute(
                "SELECT telegram_bot_token, telegram_enabled FROM app_settings WHERE id = 1"
            ).fetchone()
            if row is None:
                return "", 0
            return row["telegram_bot_token"] or "", int(row["telegram_enabled"] or 0)
        finally:
            db.close()

    def _api_call(self, token, method, payload=None, timeout=35):
        url = TELEGRAM_API.format(token=token, method=method)
        resp = requests.post(url, json=payload or {}, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("description", "Telegram API error"))
        return data

    def _send_message(self, token, chat_id, text, reply_markup=None):
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        self._api_call(token, "sendMessage", payload)

    def _edit_message(self, token, chat_id, message_id, text, reply_markup=None):
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        self._api_call(token, "editMessageText", payload)

    def _answer_callback(self, token, callback_query_id, text=""):
        self._api_call(
            token,
            "answerCallbackQuery",
            {"callback_query_id": callback_query_id, "text": text, "show_alert": False},
        )

    def _get_subscriber(self, db, chat_id):
        return db.execute(
            "SELECT * FROM telegram_subscribers WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()

    def _upsert_subscriber(self, db, chat_id, username=None):
        ts = datetime.now(ZoneInfo(self.timezone_name)).isoformat(timespec="seconds")
        existing = self._get_subscriber(db, chat_id)
        if existing is None:
            db.execute(
                """
                INSERT INTO telegram_subscribers (
                    chat_id, username, notify_all, notify_errors, notify_updated, notify_added, notify_completed, created_at, updated_at
                ) VALUES (?, ?, 1, 1, 1, 1, 1, ?, ?)
                """,
                (chat_id, username or "", ts, ts),
            )
        else:
            db.execute(
                "UPDATE telegram_subscribers SET username = ?, updated_at = ? WHERE chat_id = ?",
                (username or existing["username"] or "", ts, chat_id),
            )
        db.commit()

    def _toggle_filter(self, db, chat_id, field):
        if field not in FILTER_LABELS:
            return None
        row = self._get_subscriber(db, chat_id)
        if row is None:
            return None
        new_value = 0 if row[field] else 1
        ts = datetime.now(ZoneInfo(self.timezone_name)).isoformat(timespec="seconds")
        db.execute(
            f"UPDATE telegram_subscribers SET {field} = ?, updated_at = ? WHERE chat_id = ?",
            (new_value, ts, chat_id),
        )
        db.commit()
        return new_value

    def _settings_keyboard(self, row):
        buttons = []
        for field, label in FILTER_LABELS.items():
            enabled = bool(row[field])
            mark = "✅" if enabled else "❌"
            buttons.append(
                [{"text": f"{mark} {label}", "callback_data": f"toggle:{field}"}]
            )
        return {"inline_keyboard": buttons}

    def _settings_text(self, row):
        lines = ["<b>Фильтры уведомлений</b>", "Нажмите кнопку, чтобы включить или выключить:", ""]
        for field, label in FILTER_LABELS.items():
            mark = "✅" if row[field] else "❌"
            lines.append(f"{mark} {html.escape(label)}")
        lines.append("")
        lines.append("Команды: /start /settings /status /help")
        return "\n".join(lines)

    def _should_notify(self, subscriber, event_type):
        if subscriber["notify_all"]:
            return True
        if event_type == "error":
            return bool(subscriber["notify_errors"])
        if event_type == "updated":
            return bool(subscriber["notify_updated"])
        if event_type == "added":
            return bool(subscriber["notify_added"])
        if event_type == "completed":
            try:
                return bool(subscriber["notify_completed"])
            except (IndexError, KeyError):
                return True
        if event_type == "unchanged":
            return bool(subscriber["notify_all"])
        return False

    def notify(self, message, level, job_name=None, event_type="info"):
        token, enabled = self._get_bot_settings()
        if not enabled or not token:
            return

        db = self._connect()
        try:
            subscribers = db.execute("SELECT * FROM telegram_subscribers").fetchall()
            if not subscribers:
                return

            stamp = datetime.now(ZoneInfo(self.timezone_name)).strftime("%d.%m.%Y %H:%M:%S")
            level_icon = {"ERROR": "🔴", "INFO": "🟢", "WARNING": "🟡"}.get(level, "ℹ️")
            title = html.escape(job_name or "Torrent Manager")
            body = html.escape(message)
            text = f"{level_icon} <b>{html.escape(level)}</b> | {title}\n<code>{stamp}</code>\n\n{body}"

            for sub in subscribers:
                if not self._should_notify(sub, event_type):
                    continue
                try:
                    self._send_message(token, sub["chat_id"], text)
                except Exception as err:
                    logging.warning("Telegram notify failed for chat %s: %s", sub["chat_id"], err)
        finally:
            db.close()

    def send_test_message(self):
        token, enabled = self._get_bot_settings()
        if not token:
            raise RuntimeError("Укажите токен бота")
        if not enabled:
            raise RuntimeError("Включите Telegram-уведомления в настройках")

        db = self._connect()
        try:
            subscribers = db.execute("SELECT chat_id FROM telegram_subscribers").fetchall()
            if not subscribers:
                raise RuntimeError("Сначала отправьте боту команду /start в Telegram")
            text = "✅ <b>Torrent Manager</b>\nТестовое сообщение. Уведомления работают."
            for sub in subscribers:
                self._send_message(token, sub["chat_id"], text)
        finally:
            db.close()

    def _handle_message(self, token, message):
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            return

        text = (message.get("text") or "").strip()
        username = (chat.get("username") or chat.get("first_name") or "").strip()
        command = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""

        db = self._connect()
        try:
            if command == "/start":
                self._upsert_subscriber(db, chat_id, username)
                self._send_message(
                    token,
                    chat_id,
                    "✅ Уведомления подключены.\n\n"
                    "Откройте /settings и выберите, какие события присылать.\n"
                    "Команда /status покажет текущие фильтры.",
                )
                return

            if command in {"/settings", "/status"}:
                row = self._get_subscriber(db, chat_id)
                if row is None:
                    self._send_message(token, chat_id, "Сначала отправьте /start")
                    return
                self._send_message(
                    token,
                    chat_id,
                    self._settings_text(row),
                    reply_markup=self._settings_keyboard(row),
                )
                return

            if command == "/help":
                self._send_message(
                    token,
                    chat_id,
                    "<b>Команды бота</b>\n"
                    "/start — подключить уведомления\n"
                    "/settings — выбрать типы логов\n"
                    "/status — показать текущие фильтры\n"
                    "/help — эта справка",
                )
                return

            if text:
                self._send_message(
                    token,
                    chat_id,
                    "Неизвестная команда. Используйте /settings или /help.",
                )
        finally:
            db.close()

    def _handle_callback(self, token, callback):
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        message_id = message.get("message_id")
        data = callback.get("data") or ""
        callback_id = callback.get("id")

        if chat_id is None or not data.startswith("toggle:"):
            return

        field = data.split(":", 1)[1]
        db = self._connect()
        try:
            row = self._get_subscriber(db, chat_id)
            if row is None:
                self._answer_callback(token, callback_id, "Сначала отправьте /start")
                return
            self._toggle_filter(db, chat_id, field)
            row = self._get_subscriber(db, chat_id)
            self._edit_message(
                token,
                chat_id,
                message_id,
                self._settings_text(row),
                reply_markup=self._settings_keyboard(row),
            )
            self._answer_callback(token, callback_id, "Сохранено")
        finally:
            db.close()

    def _poll_loop(self):
        while not self._stop.is_set():
            token, enabled = self._get_bot_settings()
            if not token or not enabled:
                time.sleep(3)
                continue
            try:
                data = self._api_call(
                    token,
                    "getUpdates",
                    {"offset": self.offset, "timeout": 25, "allowed_updates": ["message", "callback_query"]},
                    timeout=35,
                )
                for update in data.get("result", []):
                    self.offset = update["update_id"] + 1
                    if "message" in update:
                        self._handle_message(token, update["message"])
                    elif "callback_query" in update:
                        self._handle_callback(token, update["callback_query"])
            except requests.RequestException as err:
                logging.warning("Telegram polling error: %s", err)
                time.sleep(5)
            except Exception as err:
                logging.warning("Telegram handler error: %s", err)
                time.sleep(3)
