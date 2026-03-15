# Personal Assistant Bot

Single-user Telegram assistant built with `aiogram`, `asyncpg`, Gemini, Google Tasks, and iCloud CalDAV.

This repo is now wired for a safer `Render Free` deployment model:

- `polling-web` is the supported runtime mode
- `ADMIN_ID` is mandatory
- all internal HTTP jobs use `X-Internal-Key`
- reminders are DB-backed and claimed via a state machine
- LLM actions are saved as drafts and require explicit confirmation

## Runtime contract

- Single-user only. The bot rejects updates from users other than `ADMIN_ID`.
- Postgres is the source of truth.
- Reminders must survive restarts and cold starts.
- Gemini may propose actions, but it must not execute them directly.
- `Render Free` is still a compromise platform. Expect delayed delivery under cold starts.

## Required environment variables

- `BOT_TOKEN`
- `DATABASE_URL`
- `ADMIN_ID`
- `INTERNAL_API_KEY`

## Common optional environment variables

- `BOT_TIMEZONE` or `APP_TIMEZONE`
- `LOG_LEVEL`
- `LOG_FORMAT`
- `GEMINI_API_KEY` or `GOOGLE_API_KEY`
- `GEMINI_LLM_MODEL`
- `GEMINI_TRANSCRIBE_MODEL`
- `GEMINI_TIMEOUT_SEC`
- `GTASKS_PERSONAL_LIST`
- `GTASKS_IDEAS_LIST`
- `ICLOUD_APPLE_ID`
- `ICLOUD_APP_PASSWORD`
- `ICLOUD_CALENDAR_URL_WORK`
- `ICLOUD_CALENDAR_URL_PERSONAL`
- `BACKUP_STORAGE_BACKEND`
- `BACKUP_RETENTION_DAYS`

## Local run

```bash
pip install -r requirements.txt
python bot.py
```

The app starts an HTTP server and runs Telegram polling in a background task.

## HTTP endpoints

- `GET /ping`
  - liveness
- `GET /health`
  - public readiness check, no sensitive details
- `GET /keepalive`
  - lightweight endpoint for Render keep-warm cron
- `GET /tick`
  - protected cron endpoint for reminders and retries
- `GET /internal/status`
  - protected operational status
- `POST /backup`
  - protected backup trigger

Protected endpoints require:

```text
X-Internal-Key: <INTERNAL_API_KEY>
```

## Render Free deployment

Recommended shape:

1. Create a Render Web Service.
2. Start command: `python bot.py`
3. Configure the required env vars.
4. Add Render Cron jobs:
   - `GET https://<host>/keepalive` every 4-5 minutes
   - `GET https://<host>/tick` with header `X-Internal-Key`
   - `POST https://<host>/backup` with header `X-Internal-Key`

Notes:

- `keepalive` is a workaround, not a guarantee.
- reminders are effectively-once at application level, not real-time guaranteed
- delayed cron execution will produce overdue delivery instead of silent loss

## LLM behavior

- Gemini output is treated as a draft
- the bot sends a preview with `Confirm` / `Cancel`
- malformed or ambiguous output should fall back to clarification
- destructive or state-changing actions should not execute without confirmation

## Data model highlights

- `reminders`
  - queue state, claim token, retries, delivery timestamps
- `pending_actions`
  - persisted LLM drafts awaiting confirmation
- `conversation_state`
  - restart-safe follow-up and bulk flow state
- `processed_updates`
  - Telegram update dedupe
- `action_journal`
  - executed actions and undo metadata
- `llm_recent_actions`
  - short-lived duplicate suppression

## Verification

Run tests with:

```bash
pytest -q
```

Current target is functional safety and restart resilience for a single-user MVP, not strict production SLA.
