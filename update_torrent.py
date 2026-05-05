#!/usr/bin/env python3
import requests
import re
import hashlib
import os
import time
import pickle
import html
from urllib.parse import urljoin
from qbittorrentapi import Client

# ===== НАСТРОЙКИ =====
RUTRACKER_URL = os.getenv("RUTRACKER_URL", "https://rutracker.org/forum/viewtopic.php?t=6845373")  # ссылка на раздачу
TORRENT_FILE = os.getenv("TORRENT_FILE", "/mnt/disk3/scripts/euphoria.torrent")           # временный файл для нового .torrent
COOKIE_FILE = os.getenv("COOKIE_FILE", "/mnt/disk3/scripts/rutracker_cookies.txt")       # для сохранения сессии
STATE_FILE = os.getenv("STATE_FILE", "/mnt/disk3/scripts/euphoria_hash.txt")            # здесь храним хэш последнего торрента

# qBittorrent
QB_HOST = os.getenv("QB_HOST", "http://127.0.0.1:8080")            # адрес qBittorrent Web UI
QB_HOST_FALLBACKS = os.getenv("QB_HOST_FALLBACKS", "http://127.0.0.1:8080,http://localhost:8080")
QB_USER = os.getenv("QB_USER", "")
QB_PASS = os.getenv("QB_PASS", "")
RUTRACKER_USER = os.getenv("RUTRACKER_USER", "")
RUTRACKER_PASS = os.getenv("RUTRACKER_PASS", "")

# Настройки добавления торрента
SAVE_PATH = os.getenv("SAVE_PATH", "/mnt/disk2/Shares/serials/Euphoria (Season 3) DV HDR10 WEB-DL 2160p")         # путь внутри контейнера qBittorrent
CATEGORY = os.getenv("CATEGORY", "Euphoria")
TORRENT_NAME_KEYWORD = os.getenv("TORRENT_NAME_KEYWORD", "Euphoria.S03")

RUTRACKER_LOGIN_URL = "https://rutracker.org/forum/login.php"
REQUEST_TIMEOUT_SECONDS = 30
# ======================

def build_qb_hosts():
    """Формируем список хостов для подключения к qBittorrent."""
    hosts = [QB_HOST]
    if QB_HOST_FALLBACKS:
        hosts.extend([h.strip() for h in QB_HOST_FALLBACKS.split(",") if h.strip()])

    # Сохраняем порядок и убираем дубликаты.
    unique_hosts = []
    seen = set()
    for host in hosts:
        if host not in seen:
            seen.add(host)
            unique_hosts.append(host)
    return unique_hosts

def connect_qbittorrent():
    """Пробуем подключиться к qBittorrent по списку хостов."""
    last_error = None
    for host in build_qb_hosts():
        qb = Client(host=host, username=QB_USER, password=QB_PASS)
        try:
            qb.auth_log_in()
            print(f"Подключение к qBittorrent успешно: {host}")
            return qb
        except Exception as e:
            last_error = e
            print(f"Не удалось подключиться к qBittorrent через {host}: {e}")
    raise Exception(f"Не удалось подключиться к qBittorrent ни по одному адресу. Последняя ошибка: {last_error}")

def get_cookies():
    """Забираем куки с rutracker.org (логинимся один раз)"""
    s = requests.Session()
    if os.path.exists(COOKIE_FILE):
        with open(COOKIE_FILE, 'rb') as f:
            s.cookies.update(pickle.load(f))
    else:
        # Логинимся
        login_data = {
            "login_username": RUTRACKER_USER,
            "login_password": RUTRACKER_PASS,
            "login": "Вход"
        }
        r = s.post(RUTRACKER_LOGIN_URL, data=login_data, timeout=REQUEST_TIMEOUT_SECONDS)
        if r.status_code != 200:
            raise Exception("Ошибка логина на RuTracker")
        if "logout" not in r.text.lower():
            raise Exception("Логин на RuTracker не подтверждён (проверьте логин/пароль)")
        os.makedirs(os.path.dirname(COOKIE_FILE), exist_ok=True)
        with open(COOKIE_FILE, 'wb') as f:
            pickle.dump(s.cookies, f)
    return s

def get_torrent_hash_from_page(session):
    """Скачиваем .torrent файл и считаем его sha1 (или md5)"""
    # Сначала получаем страницу раздачи, чтобы найти ссылку на .torrent
    resp = session.get(RUTRACKER_URL, timeout=REQUEST_TIMEOUT_SECONDS)
    resp.raise_for_status()
    page_html = html.unescape(resp.text)

    # Если пришла страница логина, значит сессия невалидна/просрочена.
    if "login_username" in page_html and "login_password" in page_html:
        raise Exception("RuTracker вернул форму логина, проверьте куки или учётные данные")

    # Ищем ссылку на скачивание в разных возможных форматах разметки.
    patterns = [
        r'["\'](/forum/dl\.php\?t=\d+)["\']',
        r'["\'](https?://[^"\']+/forum/dl\.php\?t=\d+)["\']',
        r'href=["\']([^"\']*dl\.php\?t=\d+)[^"\']*["\']',
        r'data-href=["\']([^"\']*dl\.php\?t=\d+)[^"\']*["\']',
    ]
    dl_link = None
    for pattern in patterns:
        match = re.search(pattern, page_html, flags=re.IGNORECASE)
        if match:
            raw_link = match.group(1)
            dl_link = urljoin("https://rutracker.org", raw_link)
            break

    if not dl_link:
        raise Exception("Не найдена ссылка на скачивание торрента (возможно, изменился HTML страницы)")

    # Иногда в HTML приходит "dl.php?t=..." без /forum/. Нормализуем ссылку.
    if "dl.php?t=" in dl_link and "/forum/dl.php?t=" not in dl_link:
        topic_match = re.search(r'[?&]t=(\d+)', dl_link)
        if topic_match:
            dl_link = f"https://rutracker.org/forum/dl.php?t={topic_match.group(1)}"

    # Скачиваем сам торрент
    torrent_resp = session.get(dl_link, timeout=REQUEST_TIMEOUT_SECONDS)
    torrent_resp.raise_for_status()
    torrent_data = torrent_resp.content
    if not torrent_data:
        raise Exception("Скачан пустой .torrent файл")
    # Сохраняем файл
    os.makedirs(os.path.dirname(TORRENT_FILE), exist_ok=True)
    with open(TORRENT_FILE, 'wb') as f:
        f.write(torrent_data)
    # Вычисляем хэш (например, sha1 всего файла)
    return hashlib.sha256(torrent_data).hexdigest()

def find_existing_torrent_by_name(client, name_part):
    """Ищем торрент в qbittorrent по части имени"""
    name_part = name_part.lower()
    torrents = client.torrents_info()
    for t in torrents:
        if name_part in t.name.lower():
            return t.hash
    return None

def main():
    if not all([QB_HOST, QB_USER, QB_PASS, RUTRACKER_USER, RUTRACKER_PASS]):
        print(
            "Не заданы обязательные переменные окружения: "
            "QB_HOST, QB_USER, QB_PASS, RUTRACKER_USER, RUTRACKER_PASS"
        )
        return

    # Подключаемся к qbittorrent
    try:
        qb = connect_qbittorrent()
    except Exception as e:
        print(f"Ошибка подключения к qbittorrent: {e}")
        return

    # Получаем сессию RuTracker
    sess = get_cookies()
    
    # Скачиваем новый торрент и его хэш
    try:
        new_hash = get_torrent_hash_from_page(sess)
    except Exception as e:
        print(f"Ошибка получения торрента: {e}")
        return
    
    # Проверяем, изменился ли хэш
    old_hash = None
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            old_hash = f.read().strip()
    
    if old_hash == new_hash:
        print("Торрент не изменился, выходим")
        return
    
    print("Обнаружен новый торрент! Обновляем...")
    
    # Ищем старый торрент в qbittorrent по названию раздачи (часть имени)
    old_torrent_hash = find_existing_torrent_by_name(qb, TORRENT_NAME_KEYWORD)
    if old_torrent_hash:
        # Удаляем старый торрент, но не трогаем файлы
        qb.torrents_delete(delete_files=False, torrent_hashes=old_torrent_hash)
        print(f"Удалён старый торрент {old_torrent_hash}")
        time.sleep(2)  # пауза, чтобы qb успел обработать
    
    # Добавляем новый торрент
    with open(TORRENT_FILE, 'rb') as f:
        qb.torrents_add(
            torrent_files=f,
            save_path=SAVE_PATH,
            category=CATEGORY,
            is_paused=False,
            skip_checking=False,  # qb проверит уже скачанные файлы
            content_layout="Original"
        )
    print("Новый торрент добавлен, докачка началась.")
    
    # Сохраняем новый хэш
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, 'w') as f:
        f.write(new_hash)

if __name__ == "__main__":
    main()

