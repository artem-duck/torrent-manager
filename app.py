#!/usr/bin/env python3
import hashlib
import html
import os
import pickle
import re
import sqlite3
import time
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from urllib.parse import urljoin

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, flash, g, redirect, render_template, request, session, url_for
from qbittorrentapi import Client


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data")))
DB_PATH = DATA_DIR / "app.db"
REQUEST_TIMEOUT_SECONDS = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "30"))
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")
SECRET_KEY = os.getenv("SECRET_KEY", "change-me-please")

RUTRACKER_LOGIN_URL = "https://rutracker.org/forum/login.php"

app = Flask(__name__)
app.secret_key = SECRET_KEY
scheduler = BackgroundScheduler(timezone="UTC")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def get_rutracker_session(user: str, password: str, cookie_file: Path):
    sess = requests.Session()
    if cookie_file.exists():
        with cookie_file.open("rb") as f:
            sess.cookies.update(pickle.load(f))
    else:
        login_data = {
            "login_username": user,
            "login_password": password,
            "login": "Вход",
        }
        resp = sess.post(RUTRACKER_LOGIN_URL, data=login_data, timeout=REQUEST_TIMEOUT_SECONDS)
        resp.raise_for_status()
        if "logout" not in resp.text.lower():
            raise RuntimeError("Логин RuTracker не подтверждён")
        with cookie_file.open("wb") as f:
            pickle.dump(sess.cookies, f)
    return sess


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

    cookie_file = DATA_DIR / "cookies" / f"job_{job_id}.pkl"
    torrent_file = DATA_DIR / "torrents" / f"job_{job_id}.torrent"

    qb = get_qb_client(settings)
    sess = get_rutracker_session(settings["rutracker_user"], settings["rutracker_pass"], cookie_file)

    resp = sess.get(job["rutracker_url"], timeout=REQUEST_TIMEOUT_SECONDS)
    resp.raise_for_status()
    if "login_username" in resp.text and "login_password" in resp.text:
        cookie_file.unlink(missing_ok=True)
        raise RuntimeError("RuTracker вернул страницу логина. Проверьте логин/пароль.")

    dl_link = extract_download_link(resp.text)
    if not dl_link:
        raise RuntimeError("Не найдена ссылка на .torrent (HTML страницы изменился)")

    torrent_resp = sess.get(dl_link, timeout=REQUEST_TIMEOUT_SECONDS)
    torrent_resp.raise_for_status()
    data = torrent_resp.content
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


def scheduler_tick():
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
            try:
                run_job(db, job["id"])
            except Exception as err:
                db.execute(
                    """
                    UPDATE torrent_jobs SET last_checked_at = ?, last_status = ?, last_error = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (now_iso(), "error", str(err), now_iso(), job["id"]),
                )
                db.commit()
                log_event(db, "ERROR", f"[{job['name']}] {err}", job["id"])
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
        sess = requests.Session()
        login_data = {
            "login_username": s["rutracker_user"],
            "login_password": s["rutracker_pass"],
            "login": "Вход",
        }
        resp = sess.post(RUTRACKER_LOGIN_URL, data=login_data, timeout=REQUEST_TIMEOUT_SECONDS)
        resp.raise_for_status()
        if "logout" not in resp.text.lower():
            raise RuntimeError("RuTracker не подтвердил вход")
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
    try:
        result = run_job(db, job_id)
        if result == "unchanged":
            flash("Проверка завершена: изменений нет", "success")
        else:
            flash("Проверка завершена: торрент обновлён", "success")
    except Exception as err:
        db.execute(
            "UPDATE torrent_jobs SET last_status = ?, last_error = ?, last_checked_at = ?, updated_at = ? WHERE id = ?",
            ("error", str(err), now_iso(), now_iso(), job_id),
        )
        db.commit()
        log_event(db, "ERROR", str(err), job_id)
        flash(f"Ошибка запуска: {err}", "error")
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
    init_db()
    start_scheduler()
    app.run(host="0.0.0.0", port=5000)
