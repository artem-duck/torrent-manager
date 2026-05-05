# Torrent Manager Web UI

Веб-интерфейс для управления задачами обновления RuTracker -> qBittorrent.

## Возможности

- Несколько задач с разными ссылками и `SAVE_PATH`
- Настройка логина/пароля RuTracker через UI
- Настройка `QB_HOST`, `QB_USER`, `QB_PASS` через UI
- Ручной запуск задачи
- Фоновая проверка по расписанию
- Просмотр логов в UI

## Запуск в Docker

```bash
cd /mnt/disk3/scripts
cp .env.example .env
mkdir -p data
docker compose build
docker compose up -d
```

После запуска откройте:

- `http://IP_ВАШЕГО_UNRAID:5000`

Вход по данным из `.env`:

- `ADMIN_USER`
- `ADMIN_PASSWORD`

## Первый старт

1. Откройте раздел "Настройки"
2. Заполните RuTracker и qBittorrent
3. Нажмите "Проверить RuTracker" и "Проверить qBittorrent"
4. Откройте "Задачи", добавьте минимум одну раздачу
5. Нажмите "Проверить сейчас"

## Логи

```bash
docker compose logs -f torrent_manager
```
