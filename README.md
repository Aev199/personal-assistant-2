# Personal Assistant Bot

Single-user Telegram assistant built with `aiogram`, `asyncpg`, Gemini/DeepSeek, Google Tasks, iCloud CalDAV, and an SPA-style single-message interface.

## Product model

The bot is optimized for low-friction capture rather than form filling:

- text and voice messages are classified into work tasks, personal tasks, ideas, reminders, and events;
- safe captures execute immediately and remain undoable;
- calendar events retain an explicit confirmation step during ordinary free-form use;
- unknown project references fall back to `INBOX`;
- Postgres remains the source of truth for internal tasks, reminders, runtime state, and audit data.

## Free-form lists and SPA receipts

Multi-item free-form text and voice messages use the existing batch classifier
and execution pipeline. Before classification, the original text/transcript is
retained in a durable receipt in `conversation_state` (no expiration). Each
recognized item and its pending-action ID are then saved incrementally. Incomplete
items and unavailable destinations remain in the receipt instead of disappearing
or being moved to the work Inbox. Work tasks and Google Tasks personal lists stay
separate. The explicit `/setup` import is a separate existing flow.

One SPA receipt replaces the current screen and distinguishes executed actions,
pending confirmations, errors, and unprocessed items using database statuses.
Calendar events still require confirmation; open their individual preview from
the receipt. Receipts can be reopened through **More → Records (Записи)**, including
the original input. The receipt itself is not a scheduled reminder: items lacking
required details still need clarification before they can be executed.

`ASSISTANT_RICH_MESSAGES=1` (default) enables a collapsed original-input section
using Telegram Rich Messages when supported by the installed aiogram. Rich content
is edited on the existing SPA anchor. Unsupported rich payloads retry ordinary
HTML on the same anchor; older aiogram installations use HTML directly. Set the
variable to `0` to disable rich rendering. Long source text remains available via
pagination in both modes. Voice progress also uses the SPA renderer.

References: [Rich message formatting](https://core.telegram.org/bots/api#rich-message-formatting-options),
[editing messages](https://core.telegram.org/bots/api#editmessagetext).

## Daily use

Send one task as plain text; choosing a project or due date is optional. The home
screen shows up to three tasks due before tomorrow and uses the remaining slots
(up to five total) for undated or upcoming work. This keeps unscheduled work
visible even when overdue tasks accumulate. These are internal Postgres tasks;
Google Tasks personal lists are not mirrored into this screen.

Open a task by its title, or tap the separate ✓ button to complete it. The home
screen offers the existing 30-second undo for completion. The persistent menu
keeps Today, Add, All tasks, Reminders, and capture undo; Projects, Inbox, and
administration are under More. Add stays available if the LLM is offline so
manual capture remains reachable.

## Initial population

Start with one task, or import a short list when several tasks are already on your mind.

Start with either:

- `/setup`;
- `➕ Добавить` → `Добавить списком`.

Then send a text or voice list. Two or three items are enough to start. The setup flow:

1. splits the dump into independent items;
2. classifies work, personal tasks, ideas, reminders, and events;
3. groups recurring work topics and suggests projects;
4. shows one combined preview;
5. saves everything after one confirmation.

Two save modes are available:

- `Сохранить всё` creates suggested projects and uses existing ones;
- `Без новых проектов` still uses matching existing projects, but routes new/uncertain groupings to `INBOX`.

The import is lossless by design. If Google Tasks or iCloud is unavailable or fails during import, or a reminder time is incomplete, the item is preserved as an internal Inbox task instead of being dropped.

Voice transcription requires Gemini. The resulting transcript can still be classified by DeepSeek if the Gemini classification request fails.

## LLM provider routing

Text classification uses this order:

1. Gemini primary model and its configured Gemini model fallbacks;
2. DeepSeek fallback when Gemini is unavailable, rate-limited, has an open circuit breaker, returns malformed output, or is not configured.

DeepSeek is also allowed as the only text provider. Audio transcription remains Gemini-only.

## Proactive messages

Proactive messages are quiet by default until the system has useful content:

- no morning card before onboarding is completed, unless at least five internal tasks already exist;
- no empty “day is free” card;
- weekend morning cards are disabled by default;
- a morning card is sent only when today contains tasks, overdue work, or reminders;
- evening Inbox nudges are disabled by default and require explicit opt-in;
- proactive cards replace the existing SPA anchor instead of leaving a second permanent bot message.

## Required environment variables

- `BOT_TOKEN`
- `DATABASE_URL`
- `ADMIN_ID`
- `INTERNAL_API_KEY`

At least one text LLM provider must be configured:

- `GEMINI_API_KEY` or `GOOGLE_API_KEY`; and/or
- `DEEPSEEK_API_KEY`.

## LLM environment variables

Gemini:

- `GEMINI_BASE_URL`
- `GEMINI_LLM_MODEL`
- `GEMINI_TRANSCRIBE_MODEL`
- `GEMINI_TIMEOUT_SEC`
- `GEMINI_FALLBACK_MODELS` — comma-separated Gemini model names

DeepSeek:

- `DEEPSEEK_API_KEY`
- `DEEPSEEK_BASE_URL` — default `https://api.deepseek.com`
- `DEEPSEEK_MODEL` — default `deepseek-v4-flash`
- `DEEPSEEK_TIMEOUT_SEC` — default `45`
- `DEEPSEEK_MAX_TOKENS` — default `8192`, useful for large onboarding dumps
- `DEEPSEEK_USER_ID` — optional stable anonymized caller identifier

## Assistant behavior variables

- `ASSISTANT_INSTANT_CAPTURE=1`
- `ASSISTANT_HABIT_MESSAGES=1`
- `ASSISTANT_MORNING_BRIEF_HOUR=8`
- `ASSISTANT_WEEKEND_BRIEF=0`
- `ASSISTANT_EVENING_NUDGE=0`
- `ASSISTANT_EVENING_NUDGE_HOUR=19`
- `ASSISTANT_MIN_ITEMS_FOR_HABITS=5`
- `ASSISTANT_EVENING_INBOX_THRESHOLD=3`

Set `ASSISTANT_INSTANT_CAPTURE=0` to restore confirmation cards for all ordinary LLM-originated actions. Set `ASSISTANT_HABIT_MESSAGES=0` to disable all proactive cards.

## Integration variables

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

Runtime mode is selected by `BOT_RUNTIME_MODE`:

- `webhook` — recommended on Render Free;
- `polling-web` — fallback/debug mode;
- `auto` — webhook when `WEBHOOK_URL` exists, otherwise polling-web.

## HTTP endpoints

- `GET /ping` — liveness;
- `GET /health` — readiness;
- `GET /keepalive` — Render keep-warm endpoint;
- `GET /tick` — reminders, retries, synchronization, and useful proactive cards;
- `GET /internal/status` — protected operational status;
- `POST /backup` — protected backup trigger.

Protected endpoints require:

```text
X-Internal-Key: <INTERNAL_API_KEY>
```

## Render deployment

Recommended setup:

1. Create a Render Web Service.
2. Start with `python bot.py`.
3. Configure `BOT_RUNTIME_MODE=webhook` and `WEBHOOK_URL`.
4. Add cron calls to `/keepalive`, `/tick`, and `/backup` using `X-Internal-Key` where required.

Render Free remains a compromise platform. Cold starts can delay reminders; delayed delivery is preferred over silent loss.

## Data model highlights

- `tasks` — internal work/Inbox tasks;
- `reminders` — durable queue, retry and delivery state;
- `pending_actions` — persisted execution records and event drafts;
- `conversation_state` — restart-safe follow-up and bulk state;
- `action_journal` — executed actions and undo metadata;
- `assistant_habit_state` — proactive-message dates and onboarding completion marker.

## Verification

```bash
pytest -q
```

The target is a low-friction, restart-resilient single-user assistant rather than a strict real-time SLA.
