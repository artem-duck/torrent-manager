#!/usr/bin/env python3
import hashlib
import html
import json
import logging
import os
import re
import sqlite3
import time
import threading
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, flash, g, redirect, render_template, request, session, url_for
from qbittorrentapi import Client
from werkzeug.serving import WSGIRequestHandler


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data")))
DB_PATH = DATA_DIR / "app.db"
REQUEST_TIMEOUT_SECONDS = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "60"))
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")
SECRET_KEY = os.getenv("SECRET_KEY", "change-me-please")
APP_TIMEZONE = os.getenv("APP_TIMEZONE", "Europe/Moscow")
APP_TZ = ZoneInfo(APP_TIMEZONE)

RUTRACKER_LOGIN_URL = "https://rutracker.org/forum/login.php"
RUTRACKER_BASE_URL = "https://rutracker.org"
FLARESOLVERR_URL = os.getenv("FLARESOLVERR_URL", "http://flaresolverr:8191")
FLARESOLVERR_TIMEOUT = int(os.getenv("FLARESOLVERR_TIMEOUT", "240"))
FLARESOLVERR_RETRIES = int(os.getenv("FLARESOLVERR_RETRIES", "3"))
FLARESOLVERR_STARTUP_TIMEOUT = int(os.getenv("FLARESOLVERR_STARTUP_TIMEOUT", "180"))
FLARESOLVERR_SESSION = os.getenv("FLARESOLVERR_SESSION", "torrent-manager")
RUTRACKER_STATE_FILE = DATA_DIR / "cookies" / "rutracker_session.json"

_flare_lock = threading.Lock()
_job_lock = threading.Lock()
_flare_user_agent: str | None = None

app = Flask(__name__)
app.secret_key = SECRET_KEY
scheduler = BackgroundScheduler(timezone="UTC")


class LocalTimeRequestHandler(WSGIRequestHandler):
    def log_date_time_string(self) -> str:
        return datetime.now(APP_TZ).strftime("%d/%b/%Y %H:%M:%S")


class LocalTimeFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=APP_TZ)
        return dt.strftime(datefmt or "%d.%m.%Y %H:%M:%S")


def setup_logging():
    handler = logging.StreamHandler()
    handler.setFormatter(LocalTimeFormatter("%(asctime)s %(levelname)s: %(message)s"))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def now_iso() -> str:
    return datetime.now(APP_TZ).isoformat(timespec="seconds")


def format_local_time(value: str | None) -> str:
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=APP_TZ)
        return dt.astimezone(APP_TZ).strftime("%d.%m.%Y %H:%M:%S")
    except ValueError:
        return value


app.jinja_env.filters["localtime"] = format_local_time


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "cookies").mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "torrents").mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.execute("PRAGMA journal_mode=WAL")
    cursor = db.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS app_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            rutracker_user TEXT NOT NULL DEFAULT '',
            rutracker_pass TEXT NOT NULL DEFAULT '',
            qb_host TEXT NOT NULL DEFAULT '',
            qb_user TEXT NOT NULL DEFAULT '',
            qb_pass TEXT NOT NULL DEFAULT '',
            global_check_interval_min INTEGER NOT NULL DEFAULT 30,
            updated_at TEXT NOT NULL
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS torrent_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            rutracker_url TEXT NOT NULL UNIQUE,
            save_path TEXT NOT NULL,
            category TEXT DEFAULT '',
            torrent_name_keyword TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            check_interval_min INTEGER,
            last_hash TEXT,
            last_checked_at TEXT,
            last_status TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS job_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER,
            level TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (job_id) REFERENCES torrent_jobs(id) ON DELETE CASCADE
        )
        """
    )
    ts = now_iso()
    cursor.execute("SELECT id FROM app_settings WHERE id = 1")
    if cursor.fetchone() is None:
        cursor.execute(
            """
            INSERT INTO app_settings (
                id, rutracker_user, rutracker_pass, qb_host, qb_user, qb_pass, global_check_interval_min, updated_at
            ) VALUES (1, '', '', 'http://127.0.0.1:8080', 'admin', '', 30, ?)
            """,
            (ts,),
        )
    db.commit()
    db.close()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


def log_event(db, level: str, message: str, job_id=None):
    db.execute(
        "INSERT INTO job_logs (job_id, level, message, created_at) VALUES (?, ?, ?, ?)",
        (job_id, level, message, now_iso()),
    )
    db.commit()


def get_settings(db):
    return db.execute("SELECT * FROM app_settings WHERE id = 1").fetchone()


def get_qb_client(settings):
    qb = Client(
        host=settings["qb_host"],
        username=settings["qb_user"],
        password=settings["qb_pass"],
    )
    qb.auth_log_in()
    return qb


def _load_rutracker_state() -> dict:
    if RUTRACKER_STATE_FILE.exists():
        try:
            data = json.loads(RUTRACKER_STATE_FILE.read_text())
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    for legacy in (DATA_DIR / "cookies").glob("job_*_state.json"):
        try:
            legacy_data = json.loads(legacy.read_text())
            if legacy_data:
                state = {"cookies": legacy_data if isinstance(legacy_data, list) else legacy_data.get("cookies", [])}
                _save_rutracker_state(state)
                return state
        except json.JSONDecodeError:
            continue
    return {"cookies": [], "user_agent": None}


def _save_rutracker_state(state: dict):
    RUTRACKER_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    cookies = _dedupe_cookies(_normalize_cookies(state.get("cookies")))
    payload = {"cookies": cookies, "user_agent": state.get("user_agent") or _flare_user_agent}
    RUTRACKER_STATE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2))


def _dedupe_cookies(cookies: list) -> list:
    unique = {}
    for cookie in cookies:
        key = (cookie.get("name"), cookie.get("domain"), cookie.get("path"))
        unique[key] = cookie
    return list(unique.values())


def _normalize_cookies(cookies) -> list:
    if not cookies:
        return []
    if isinstance(cookies, list):
        return cookies
    if isinstance(cookies, dict):
        return [{"name": name, "value": value} for name, value in cookies.items()]
    return []


def _flare_is_ready() -> bool:
    try:
        resp = requests.post(
            f"{FLARESOLVERR_URL}/v1",
            json={"cmd": "sessions.list"},
            timeout=10,
        )
        if resp.status_code != 200:
            return False
        data = resp.json()
        return data.get("status") == "ok"
    except requests.RequestException:
        return False


def _wait_for_flaresolverr(timeout: int | None = None):
    deadline = time.time() + (timeout or FLARESOLVERR_STARTUP_TIMEOUT)
    while time.time() < deadline:
        if _flare_is_ready():
            return
        time.sleep(3)
    raise RuntimeError(
        f"FlareSolverr недоступен по адресу {FLARESOLVERR_URL}. "
        f"Подождите ~2 мин после перезапуска и попробуйте снова."
    )


def _flare_call(payload: dict) -> dict:
    global _flare_user_agent
    http_timeout = min(30, FLARESOLVERR_TIMEOUT + 60) if payload.get("cmd") in {"sessions.list", "sessions.create", "sessions.destroy"} else FLARESOLVERR_TIMEOUT + 60
    resp = requests.post(f"{FLARESOLVERR_URL}/v1", json=payload, timeout=http_timeout)
    if resp.status_code == 500:
        message = resp.json().get("message", resp.text)
        if payload.get("cmd") == "sessions.destroy" and "doesn't exist" in message.lower():
            return {}
        raise RuntimeError(message)
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "ok":
        raise RuntimeError(data.get("message", "FlareSolverr вернул ошибку"))
    if "solution" in data:
        solution = data["solution"]
        if solution.get("userAgent"):
            _flare_user_agent = solution["userAgent"]
        return solution
    return data


def _flare_session_ids() -> list[str]:
    data = _flare_call({"cmd": "sessions.list"})
    return data.get("sessions", [])


def _flare_ensure_session_unlocked():
    if FLARESOLVERR_SESSION not in _flare_session_ids():
        _flare_call({"cmd": "sessions.create", "session": FLARESOLVERR_SESSION})


def _flare_destroy_session():
    global _flare_user_agent
    with _flare_lock:
        try:
            if FLARESOLVERR_SESSION in _flare_session_ids():
                _flare_call({"cmd": "sessions.destroy", "session": FLARESOLVERR_SESSION})
        except Exception:
            pass
        _flare_user_agent = None


def _flare_validate_page(response: str, url: str):
    if not response or len(response) < 200:
        raise RuntimeError("FlareSolverr вернул пустой ответ")
    title_lower = ""
    match = re.search(r"<title[^>]*>(.*?)</title>", response, re.IGNORECASE | re.DOTALL)
    if match:
        title_lower = html.unescape(match.group(1)).strip().lower()
    bad_markers = (
        "new tab",
        "just a moment",
        "attention required",
        "checking your browser",
        "please wait",
        "один момент",
    )
    if any(marker in title_lower for marker in bad_markers):
        raise RuntimeError(f"Cloudflare не пройден для {url} (страница: {title_lower or 'unknown'})")


def _page_title_lower(text: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    return html.unescape(match.group(1)).strip().lower()


def _is_cloudflare_page(text: str) -> bool:
    title_lower = _page_title_lower(text)
    bad_markers = (
        "new tab",
        "just a moment",
        "attention required",
        "checking your browser",
        "please wait",
        "один момент",
    )
    return any(marker in title_lower for marker in bad_markers)


def _is_login_page(text: str) -> bool:
    return "login_username" in text and "login_password" in text


def _has_rutracker_session(state: dict) -> bool:
    names = {cookie.get("name") for cookie in _normalize_cookies(state.get("cookies"))}
    return "bb_session" in names and "cf_clearance" in names


def _flare_get(url, cookies=None, destroy_session_on_fail=True):
    last_err = None
    for attempt in range(1, FLARESOLVERR_RETRIES + 1):
        try:
            _wait_for_flaresolverr(timeout=60)
            with _flare_lock:
                _flare_ensure_session_unlocked()
                payload = {
                    "cmd": "request.get",
                    "url": url,
                    "session": FLARESOLVERR_SESSION,
                    "maxTimeout": FLARESOLVERR_TIMEOUT * 1000,
                }
                normalized = _normalize_cookies(cookies)
                if normalized:
                    payload["cookies"] = normalized
                solution = _flare_call(payload)
            response = solution.get("response", "")
            _flare_validate_page(response, url)
            return solution
        except Exception as err:
            last_err = err
            logging.warning("FlareSolverr GET %s attempt %s/%s failed: %s", url, attempt, FLARESOLVERR_RETRIES, err)
            if destroy_session_on_fail and attempt == FLARESOLVERR_RETRIES:
                _flare_destroy_session()
            time.sleep(min(5 * attempt, 20))
    raise RuntimeError(f"FlareSolverr не смог решить Cloudflare после {FLARESOLVERR_RETRIES} попыток: {last_err}")


def _flare_post(url, post_data, cookies=None, destroy_session_on_fail=True):
    last_err = None
    for attempt in range(1, FLARESOLVERR_RETRIES + 1):
        try:
            _wait_for_flaresolverr(timeout=60)
            with _flare_lock:
                _flare_ensure_session_unlocked()
                payload = {
                    "cmd": "request.post",
                    "url": url,
                    "postData": post_data,
                    "session": FLARESOLVERR_SESSION,
                    "maxTimeout": FLARESOLVERR_TIMEOUT * 1000,
                }
                normalized = _normalize_cookies(cookies)
                if normalized:
                    payload["cookies"] = normalized
                solution = _flare_call(payload)
            response = solution.get("response", "")
            _flare_validate_page(response, url)
            return solution
        except Exception as err:
            last_err = err
            logging.warning("FlareSolverr POST %s attempt %s/%s failed: %s", url, attempt, FLARESOLVERR_RETRIES, err)
            if destroy_session_on_fail and attempt == FLARESOLVERR_RETRIES:
                _flare_destroy_session()
            time.sleep(min(5 * attempt, 20))
    raise RuntimeError(f"FlareSolverr не смог решить Cloudflare после {FLARESOLVERR_RETRIES} попыток: {last_err}")


def _requests_with_rutracker_state(url: str, state: dict) -> requests.Response:
    session = requests.Session()
    user_agent = state.get("user_agent") or _flare_user_agent
    if user_agent:
        session.headers["User-Agent"] = user_agent
    for cookie in _normalize_cookies(state.get("cookies")):
        session.cookies.set(
            cookie["name"],
            cookie["value"],
            domain=cookie.get("domain") or ".rutracker.org",
            path=cookie.get("path") or "/",
        )
    resp = session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
    resp.raise_for_status()
    return resp


def _is_torrent_bytes(data: bytes) -> bool:
    return bool(data) and data[:1] == b"d"


def rutracker_login(user: str, password: str):
    solution = _flare_get(RUTRACKER_BASE_URL, destroy_session_on_fail=False)
    cookies = solution.get("cookies", [])

    if "logout" not in solution.get("response", "").lower():
        post_data = f"login_username={user}&login_password={password}&login=%D0%92%D1%85%D0%BE%D0%B4"
        solution = _flare_post(RUTRACKER_LOGIN_URL, post_data, cookies=cookies, destroy_session_on_fail=False)
        cookies = solution.get("cookies", cookies)
        if "logout" not in solution.get("response", "").lower():
            raise RuntimeError("Логин RuTracker не подтверждён")

    _save_rutracker_state({"cookies": cookies, "user_agent": solution.get("userAgent") or _flare_user_agent})


def ensure_rutracker_session(settings):
    state = _load_rutracker_state()
    if _has_rutracker_session(state):
        try:
            resp = _requests_with_rutracker_state(f"{RUTRACKER_BASE_URL}/forum/index.php", state)
            if "logout" in resp.text.lower() and not _is_cloudflare_page(resp.text):
                logging.info("RuTracker session is valid")
                return state
        except requests.RequestException as err:
            logging.info("Saved RuTracker session check failed: %s", err)

    logging.info("RuTracker login required")
    rutracker_login(settings["rutracker_user"], settings["rutracker_pass"])
    return _load_rutracker_state()


def get_rutracker_content(url: str, state: dict) -> str:
    try:
        resp = _requests_with_rutracker_state(url, state)
        text = resp.text
        if not _is_cloudflare_page(text) and not _is_login_page(text):
            logging.info("RuTracker page loaded directly: %s", url)
            return text
        logging.info("Direct RuTracker request blocked, using FlareSolverr: %s", url)
    except requests.RequestException as err:
        logging.info("Direct RuTracker request failed, using FlareSolverr for %s: %s", url, err)

    solution = _flare_get(url)
    cookies = solution.get("cookies") or state.get("cookies")
    _save_rutracker_state({"cookies": cookies, "user_agent": solution.get("userAgent") or state.get("user_agent")})
    return solution.get("response", "")


def download_rutracker_file(url: str, state: dict) -> bytes:
    try:
        resp = _requests_with_rutracker_state(url, state)
        if _is_torrent_bytes(resp.content):
            logging.info("Torrent downloaded directly: %s", url)
            return resp.content
    except requests.RequestException as err:
        logging.info("Direct torrent download failed for %s: %s", url, err)

    solution = _flare_get(url)
    cookies = solution.get("cookies") or state.get("cookies")
    _save_rutracker_state({"cookies": cookies, "user_agent": solution.get("userAgent") or state.get("user_agent")})

    resp = _requests_with_rutracker_state(url, _load_rutracker_state())
    if _is_torrent_bytes(resp.content):
        return resp.content

    raise RuntimeError("Скачан не .torrent файл (возможно, сессия RuTracker истекла)")


def extract_download_link(page_html: str):
    content = html.unescape(page_html)
    patterns = [
        r'["\'](/forum/dl\.php\?t=\d+)["\']',
        r'["\'](https?://[^"\']+/forum/dl\.php\?t=\d+)["\']',
        r'href=["\']([^"\']*dl\.php\?t=\d+)[^"\']*["\']',
        r'data-href=["\']([^"\']*dl\.php\?t=\d+)[^"\']*["\']',
    ]
    for pattern in patterns:
        m = re.search(pattern, content, flags=re.IGNORECASE)
        if m:
            link = urljoin("https://rutracker.org", m.group(1))
            if "dl.php?t=" in link and "/forum/dl.php?t=" not in link:
                tm = re.search(r"[?&]t=(\d+)", link)
                if tm:
                    return f"https://rutracker.org/forum/dl.php?t={tm.group(1)}"
            return link
    return None


def find_existing_torrent_by_name(qb, name_part: str):
    key = name_part.lower().strip()
    for torrent in qb.torrents_info():
        if key and key in torrent.name.lower():
            return torrent.hash
    return None


def run_job(db, job_id: int):
    job = db.execute("SELECT * FROM torrent_jobs WHERE id = ?", (job_id,)).fetchone()
    settings = get_settings(db)
    if job is None:
        raise RuntimeError("Задача не найдена")
    if not settings["rutracker_user"] or not settings["rutracker_pass"]:
        raise RuntimeError("Заполните логин/пароль RuTracker в настройках")
    if not settings["qb_host"] or not settings["qb_user"] or not settings["qb_pass"]:
        raise RuntimeError("Заполните настройки qBittorrent")

    torrent_file = DATA_DIR / "torrents" / f"job_{job_id}.torrent"

    state = ensure_rutracker_session(settings)
    page_html = get_rutracker_content(job["rutracker_url"], state)
    state = _load_rutracker_state()

    if _is_login_page(page_html):
        logging.info("RuTracker session expired, re-login required")
        RUTRACKER_STATE_FILE.unlink(missing_ok=True)
        rutracker_login(settings["rutracker_user"], settings["rutracker_pass"])
        state = _load_rutracker_state()
        page_html = get_rutracker_content(job["rutracker_url"], state)

    if _is_cloudflare_page(page_html):
        raise RuntimeError("Cloudflare не пройден для страницы раздачи")

    if page_html:
        debug_file = DATA_DIR / "debug" / f"job_{job_id}_last.html"
        debug_file.parent.mkdir(parents=True, exist_ok=True)
        debug_file.write_text(page_html, encoding="utf-8")

    dl_link = extract_download_link(page_html or "")
    if not dl_link:
        raise RuntimeError("Не найдена ссылка на .torrent (HTML страницы изменился или Cloudflare не пройден)")

    data = download_rutracker_file(dl_link, state)
    if not data:
        raise RuntimeError("Скачан пустой торрент-файл")
    torrent_file.write_bytes(data)
    new_hash = hashlib.sha256(data).hexdigest()

    if job["last_hash"] == new_hash:
        db.execute(
            "UPDATE torrent_jobs SET last_checked_at = ?, last_status = ?, last_error = NULL, updated_at = ? WHERE id = ?",
            (now_iso(), "unchanged", now_iso(), job_id),
        )
        db.commit()
        log_event(db, "INFO", f"[{job['name']}] Торрент не изменился", job_id)
        return "unchanged"

    qb = get_qb_client(settings)
    old_hash = find_existing_torrent_by_name(qb, job["torrent_name_keyword"])
    if old_hash:
        qb.torrents_delete(delete_files=False, torrent_hashes=old_hash)
        time.sleep(1)

    with torrent_file.open("rb") as f:
        qb.torrents_add(
            torrent_files=f,
            save_path=job["save_path"],
            category=job["category"] or "",
            is_paused=False,
            skip_checking=False,
            content_layout="Original",
        )

    db.execute(
        """
        UPDATE torrent_jobs
        SET last_hash = ?, last_checked_at = ?, last_status = ?, last_error = NULL, updated_at = ?
        WHERE id = ?
        """,
        (new_hash, now_iso(), "updated", now_iso(), job_id),
    )
    db.commit()
    log_event(db, "INFO", f"[{job['name']}] Торрент обновлён и добавлен в qBittorrent", job_id)
    return "updated"


def _run_job_thread(job_id, job_name):
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    try:
        with _job_lock:
            run_job(db, job_id)
    except Exception as err:
        db.execute(
            "UPDATE torrent_jobs SET last_checked_at = ?, last_status = ?, last_error = ?, updated_at = ? WHERE id = ?",
            (now_iso(), "error", str(err), now_iso(), job_id),
        )
        db.commit()
        log_event(db, "ERROR", f"[{job_name}] {err}", job_id)
    finally:
        db.close()


def scheduler_tick():
    if _job_lock.locked():
        return
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    try:
        settings = db.execute("SELECT * FROM app_settings WHERE id = 1").fetchone()
        jobs = db.execute("SELECT * FROM torrent_jobs WHERE enabled = 1").fetchall()
        now_ts = datetime.now(timezone.utc).timestamp()
        for job in jobs:
            interval = job["check_interval_min"] or settings["global_check_interval_min"] or 30
            last_checked = job["last_checked_at"]
            should_run = True
            if last_checked:
                try:
                    last_ts = datetime.fromisoformat(last_checked).timestamp()
                    should_run = now_ts - last_ts >= interval * 60
                except ValueError:
                    should_run = True
            if not should_run:
                continue
            t = threading.Thread(target=_run_job_thread, args=(job["id"], job["name"]), daemon=True)
            t.start()
    finally:
        db.close()


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if username == ADMIN_USER and password == ADMIN_PASSWORD:
            session["logged_in"] = True
            return redirect(url_for("jobs"))
        flash("Неверный логин или пароль", "error")
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    return redirect(url_for("jobs"))


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    db = get_db()
    if request.method == "POST":
        db.execute(
            """
            UPDATE app_settings
            SET rutracker_user = ?, rutracker_pass = ?, qb_host = ?, qb_user = ?, qb_pass = ?, global_check_interval_min = ?, updated_at = ?
            WHERE id = 1
            """,
            (
                request.form.get("rutracker_user", "").strip(),
                request.form.get("rutracker_pass", "").strip(),
                request.form.get("qb_host", "").strip(),
                request.form.get("qb_user", "").strip(),
                request.form.get("qb_pass", "").strip(),
                int(request.form.get("global_check_interval_min", "30")),
                now_iso(),
            ),
        )
        db.commit()
        flash("Настройки сохранены", "success")
        return redirect(url_for("settings"))
    return render_template("settings.html", settings=get_settings(db))


@app.route("/settings/test-rutracker", methods=["POST"])
@login_required
def test_rutracker():
    db = get_db()
    s = get_settings(db)
    try:
        RUTRACKER_STATE_FILE.unlink(missing_ok=True)
        _flare_destroy_session()
        rutracker_login(s["rutracker_user"], s["rutracker_pass"])
        flash("RuTracker: вход успешен", "success")
    except Exception as err:
        flash(f"RuTracker: ошибка проверки ({err})", "error")
    return redirect(url_for("settings"))


@app.route("/settings/test-qb", methods=["POST"])
@login_required
def test_qb():
    db = get_db()
    s = get_settings(db)
    try:
        qb = get_qb_client(s)
        qb.app_version()
        flash("qBittorrent: подключение успешно", "success")
    except Exception as err:
        flash(f"qBittorrent: ошибка подключения ({err})", "error")
    return redirect(url_for("settings"))


@app.route("/jobs")
@login_required
def jobs():
    db = get_db()
    jobs_data = db.execute("SELECT * FROM torrent_jobs ORDER BY id DESC").fetchall()
    return render_template("jobs.html", jobs=jobs_data)


@app.route("/jobs", methods=["POST"])
@login_required
def create_job():
    db = get_db()
    try:
        db.execute(
            """
            INSERT INTO torrent_jobs (
                name, rutracker_url, save_path, category, torrent_name_keyword, enabled, check_interval_min,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request.form.get("name", "").strip(),
                request.form.get("rutracker_url", "").strip(),
                request.form.get("save_path", "").strip(),
                request.form.get("category", "").strip(),
                request.form.get("torrent_name_keyword", "").strip(),
                1 if request.form.get("enabled") == "on" else 0,
                int(request.form["check_interval_min"]) if request.form.get("check_interval_min") else None,
                now_iso(),
                now_iso(),
            ),
        )
        db.commit()
        flash("Задача добавлена", "success")
    except sqlite3.IntegrityError:
        flash("Такая ссылка уже есть в списке", "error")
    except Exception as err:
        flash(f"Ошибка добавления: {err}", "error")
    return redirect(url_for("jobs"))


@app.route("/jobs/<int:job_id>/update", methods=["POST"])
@login_required
def update_job(job_id):
    db = get_db()
    db.execute(
        """
        UPDATE torrent_jobs
        SET name = ?, rutracker_url = ?, save_path = ?, category = ?, torrent_name_keyword = ?, enabled = ?, check_interval_min = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            request.form.get("name", "").strip(),
            request.form.get("rutracker_url", "").strip(),
            request.form.get("save_path", "").strip(),
            request.form.get("category", "").strip(),
            request.form.get("torrent_name_keyword", "").strip(),
            1 if request.form.get("enabled") == "on" else 0,
            int(request.form["check_interval_min"]) if request.form.get("check_interval_min") else None,
            now_iso(),
            job_id,
        ),
    )
    db.commit()
    flash("Задача обновлена", "success")
    return redirect(url_for("jobs"))


@app.route("/jobs/<int:job_id>/delete", methods=["POST"])
@login_required
def delete_job(job_id):
    db = get_db()
    db.execute("DELETE FROM torrent_jobs WHERE id = ?", (job_id,))
    db.commit()
    flash("Задача удалена", "success")
    return redirect(url_for("jobs"))


@app.route("/jobs/<int:job_id>/run", methods=["POST"])
@login_required
def run_job_now(job_id):
    db = get_db()
    job = db.execute("SELECT name FROM torrent_jobs WHERE id = ?", (job_id,)).fetchone()
    if job is None:
        flash("Задача не найдена", "error")
        return redirect(url_for("jobs"))
    if _job_lock.locked():
        flash("Уже выполняется другая проверка. Дождитесь завершения и смотрите логи.", "error")
        return redirect(url_for("jobs"))
    threading.Thread(target=_run_job_thread, args=(job_id, job["name"]), daemon=True).start()
    flash(f"Проверка «{job['name']}» запущена в фоне. Результат появится в логах.", "success")
    return redirect(url_for("jobs"))


@app.route("/logs")
@login_required
def logs():
    db = get_db()
    logs_data = db.execute(
        """
        SELECT l.*, j.name AS job_name
        FROM job_logs l
        LEFT JOIN torrent_jobs j ON j.id = l.job_id
        ORDER BY l.id DESC
        LIMIT 300
        """
    ).fetchall()
    return render_template("logs.html", logs=logs_data)


def start_scheduler():
    if not scheduler.running:
        scheduler.add_job(scheduler_tick, "interval", minutes=1, id="jobs_tick", replace_existing=True)
        scheduler.start()


if __name__ == "__main__":
    setup_logging()
    init_db()
    logging.info("Ожидание готовности FlareSolverr...")
    try:
        _wait_for_flaresolverr()
        logging.info("FlareSolverr готов")
    except RuntimeError as err:
        logging.warning("%s", err)
    start_scheduler()
    app.run(host="0.0.0.0", port=5000, request_handler=LocalTimeRequestHandler)
