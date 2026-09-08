# Веб-интерфейс Torrent Manager

Управление задачами RuTracker → qBittorrent и уведомлениями в Telegram.

## Возможности

- Несколько задач с разными ссылками и `SAVE_PATH`
- Логин и пароль RuTracker в настройках
- `QB_HOST`, `QB_USER`, `QB_PASS` в настройках
- Ручная проверка и фоновое расписание
- Логи в веб-интерфейсе
- Telegram-бот: токен в настройках, фильтры командой `/settings`
- Уведомление, когда qBittorrent докачал торрент до 100%

## Запуск в Docker

```bash
cd /mnt/disk1/isos/torrent-manager
cp .env.example .env
mkdir -p data
docker compose up -d --build
```

Откройте `http://IP_ВАШЕГО_СЕРВЕРА:5000`. Вход — `ADMIN_USER` и `ADMIN_PASSWORD` из `.env`.

## Первый старт

1. **Настройки** — RuTracker и qBittorrent, кнопки проверки.
2. Токен Telegram-бота, галочка уведомлений, **Сохранить**.
3. В Telegram боту: `/start`, затем `/settings`.
4. **Задачи** — добавьте раздачу, нажмите «Проверить сейчас».

## Логи контейнера

```bash
docker compose logs -f torrent_manager
```
