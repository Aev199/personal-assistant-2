# Personal Assistant Bot (aiogram 3 + asyncpg)

Проект приведён к «продакшн»-структуре: Telegram-логика отделена от HTTP-инфраструктуры, зависимости централизованы через контейнер `AppDeps`, legacy-монолит вынесен в архив `legacy_src/`.

## Как запустить

```bash
python bot.py
```

По умолчанию это **webhook-воркер** (aiohttp + aiogram webhook).

## Обязательные переменные окружения

- `BOT_TOKEN` — токен бота
- `DATABASE_URL` — строка подключения Postgres (asyncpg)
- `WEBHOOK_URL` **или** `RENDER_EXTERNAL_URL` — публичный base URL сервиса (например, `https://my-bot.onrender.com`)

## Рекомендуемые переменные

- `WEBHOOK_PATH` — путь вебхука (по умолчанию `/webhook`)
- `ADMIN_ID` — Telegram user id админа (0 = отключено)
- `BOT_TIMEZONE` — таймзона приложения (по умолчанию `Europe/Moscow`) ✅
- `TZ` — fallback таймзона (используется только если не `UTC`; некоторые хостинги ставят `TZ=UTC` по умолчанию)

## HTTP эндпоинты

- `GET /ping` — liveness (процесс жив)
- `GET /health` — readiness (готовность: **зависит только от БД**; интеграции могут быть `degraded`)
- `GET /tick?key=...` — cron-тик (reminders/ретраи/сервисные задачи)
- `POST /backup?key=...` — backup БД

### Защита tick/backup

По умолчанию `/tick` и `/backup` закрыты.

- `TICK_SECRET` — **обязателен** (если не задан, endpoints вернут 403)
- `ALLOW_PUBLIC_TICK=1` — разрешить tick/backup без секрета (не рекомендуется)

Также используется **Postgres advisory lock**, чтобы tick/backup не выполнялись параллельно на разных воркерах/инстансах.

## Логирование

- `LOG_FORMAT=json|plain` (по умолчанию `json`)
- `LOG_LEVEL=DEBUG|INFO|WARNING|ERROR` (по умолчанию `INFO`)

Логи редактируют очевидные секреты (token/password/authorization и т.п.).

## Бэкапы (опционально)

`BACKUP_STORAGE_BACKEND` выбирает backend:

- `s3`:
  - `AWS_S3_BUCKET`, `AWS_S3_REGION`
- `dropbox`:
  - `DROPBOX_ACCESS_TOKEN`, `DROPBOX_BACKUP_PATH`
- `gcs`:
  - `GCS_BUCKET`, `GCS_PROJECT_ID`, `GCS_CREDENTIALS_JSON`

Параметры:
- `BACKUP_RETENTION_DAYS` (по умолчанию `30`)

## Render (рекомендуемый деплой)

1. Создай **Web Service**
2. Start Command: `python bot.py`
3. Переменные окружения: минимум из раздела выше
4. Cron jobs (Render Cron / внешний cron):
   - `GET https://<host>/tick?key=<TICK_SECRET>`
   - `POST https://<host>/backup?key=<TICK_SECRET>`

## Архив legacy

`legacy_src/app_monolith.py` сохранён только как история/референс и **не используется** в прод-рантайме.


## Deploy

### Requirements
- Python 3.11+
- Postgres (DATABASE_URL)

### Install
```bash
pip install -r requirements.txt
# optional cloud backends:
# pip install -r requirements-optional.txt
```

### Run locally
```bash
export BOT_TOKEN=...
export DATABASE_URL=postgresql://...
python bot.py
```

### Production notes
- By default startup will **fail fast** if DB schema bootstrap fails.
  To disable (not recommended): `SCHEMA_BOOTSTRAP_STRICT=0`
- HTTP server listens on `PORT` (default 10000).
