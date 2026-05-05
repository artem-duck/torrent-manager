# Torrent Manager (RuTracker -> qBittorrent)

Open-source project for automatic torrent updates from RuTracker into qBittorrent.

Includes:
- `app.py` - Flask Web UI with multiple jobs, logs, and scheduler.
- `update_torrent.py` - standalone script for a single feed/job.

## Features

- Manage multiple RuTracker jobs from a web interface.
- Store state and logs in SQLite (`data/app.db`).
- Run checks manually or automatically on schedule.
- Replace old torrent in qBittorrent while keeping files.
- Docker-first deployment.

## Requirements

- Python 3.12+ (for local run), or Docker + Docker Compose.
- qBittorrent with enabled Web UI API.
- Valid RuTracker account.

## Quick Start (Docker)

1. Create environment file:
   - `cp .env.example .env`
2. Set secure values in `.env`:
   - `SECRET_KEY`
   - `ADMIN_USER`
   - `ADMIN_PASSWORD`
3. Start:
   - `docker compose up -d --build`
4. Open:
   - `http://localhost:5000`

## Environment Variables (Web UI)

See `.env.example`:
- `SECRET_KEY`
- `ADMIN_USER`
- `ADMIN_PASSWORD`
- `DATA_DIR`
- `REQUEST_TIMEOUT_SECONDS`

## Standalone Script Usage

`update_torrent.py` expects environment variables:
- `QB_HOST`
- `QB_HOST_FALLBACKS` (optional, comma-separated)
- `QB_USER`
- `QB_PASS`
- `RUTRACKER_USER`
- `RUTRACKER_PASS`
- optional paths and job settings (`RUTRACKER_URL`, `SAVE_PATH`, etc.)

Example:

```bash
export QB_HOST="http://127.0.0.1:8080"
export QB_USER="admin"
export QB_PASS="your_password"
export RUTRACKER_USER="your_login"
export RUTRACKER_PASS="your_password"
python update_torrent.py
```

## Local Run (without Docker)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

## Security Notes

- Do not commit real credentials.
- Keep `.env` private (only `.env.example` should be versioned).
- Rotate any secrets that were previously hardcoded in local files.

## License

MIT License. See `LICENSE`.
