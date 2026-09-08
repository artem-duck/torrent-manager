# Torrent Manager (RuTracker → qBittorrent)

Открытый проект для автоматического обновления торрентов с RuTracker и добавления их в qBittorrent.

Есть веб-интерфейс, планировщик проверок и **Telegram-бот**: ошибки, замена раздачи, добавление в клиент и уведомление, когда qBittorrent скачал файл на 100%.

В репозитории:

- `app.py` — веб-интерфейс (Flask): задачи, настройки, логи, расписание.
- `telegram_bot.py` — встроенный Telegram-бот уведомлений.
- `update_torrent.py` — отдельный скрипт для одной раздачи.

## Возможности

- Несколько задач RuTracker из браузера (своя ссылка и `SAVE_PATH`).
- Логин RuTracker и доступ к qBittorrent задаются в настройках UI.
- Обход Cloudflare через FlareSolverr, обычные проверки идут по cookies без браузера.
- Проверка вручную или по расписанию.
- Замена старого торрента в qBittorrent без удаления уже скачанных файлов.
- **Telegram-бот**: токен в настройках сайта, фильтры в самом боте (`/settings`).
- Уведомление, когда новая серия (торрент) скачана на 100%.
- Запуск через Docker Compose.

## Требования

- Docker и Docker Compose (или Python 3.12+ для локального запуска).
- qBittorrent с включённым Web UI API.
- Аккаунт RuTracker.
- Для уведомлений: бот от [@BotFather](https://t.me/BotFather).

## Быстрый старт (Docker)

1. Скопируйте окружение: `cp .env.example .env`
2. Задайте в `.env` свои `SECRET_KEY`, `ADMIN_USER`, `ADMIN_PASSWORD`
3. Запуск: `docker compose up -d --build`
4. Откройте `http://localhost:5000` (или IP сервера и порт `5000`)

## Telegram-бот

1. Создайте бота у [@BotFather](https://t.me/BotFather) и скопируйте токен.
2. В веб-интерфейсе: **Настройки** → вставьте токен → включите уведомления → **Сохранить**.
3. Напишите боту `/start`.
4. Команда `/settings` — какие события присылать:
   - все логи;
   - ошибки;
   - замена торрента;
   - добавление в qBittorrent;
   - **скачивание завершено (100%)**.
5. Кнопка **Проверить Telegram** отправляет тестовое сообщение.

## Переменные окружения

См. `.env.example`: `SECRET_KEY`, `ADMIN_USER`, `ADMIN_PASSWORD`, `DATA_DIR`, `APP_TIMEZONE`, таймауты FlareSolverr.

Токен Telegram хранится в настройках веб-UI (база `data/app.db`), не в `.env`.

## Автономный скрипт

`update_torrent.py` читает переменные `QB_HOST`, `QB_USER`, `QB_PASS`, `RUTRACKER_USER`, `RUTRACKER_PASS` и при необходимости `RUTRACKER_URL`, `SAVE_PATH`.

```bash
export QB_HOST="http://127.0.0.1:8080"
export QB_USER="admin"
export QB_PASS="your_password"
export RUTRACKER_USER="your_login"
export RUTRACKER_PASS="your_password"
python update_torrent.py
```

## Запуск без Docker

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

## Безопасность

- Не коммитьте пароли и `.env`.
- В git должен быть только `.env.example`.
- Смените пароли, если они когда-либо светились в логах.

## Лицензия

MIT. Подробности в файле `LICENSE`.
