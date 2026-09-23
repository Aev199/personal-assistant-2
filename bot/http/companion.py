"""HTTP API shared by native Assistant clients.

The backend owns task selection and mutations. iOS, widgets, shortcuts and
Telegram are interfaces over the same data; legacy /companion routes remain
as compatibility aliases while native clients move to canonical /api/v1 routes.
"""

from __future__ import annotations

import asyncio
import os
import secrets
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import asyncpg
from aiohttp import web

from bot.db import db_add_event, ensure_inbox_project_id
from bot.tz import resolve_tz_name, to_db_utc
from bot.services.native_intake import process_native_capture, confirm_native_action, cancel_native_action
from bot.services.ideas import list_active_ideas, promote_idea, archive_idea
from bot.services.calendar_today import TodayCalendarSnapshot, fetch_today_calendar
from bot.services.reminders import mark_telegram_reminder_snoozed, snooze_reminder
from bot.db.runtime_state import get_conversation_state, set_conversation_state, clear_conversation_state


MAX_CAPTURE_LEN = 2000
MAX_VOICE_BYTES = 8 * 1024 * 1024
ATTENTION_TASK_LIMIT = 5
ATTENTION_URGENT_LIMIT = 3
ATTENTION_DISMISSED_EVENTS_FLOW = "attention_dismissed_events"


def _configured_token() -> str:
    return (
        os.getenv("ASSISTANT_API_TOKEN")
        or os.getenv("COMPANION_API_TOKEN")
        or ""
    ).strip()


def _configured_widget_token() -> str:
    return (os.getenv("COMPANION_WIDGET_TOKEN") or "").strip()


def _authorized(request: web.Request, *, allow_widget: bool = False) -> bool:
    accepted = [_configured_token()]
    if allow_widget:
        accepted.append(_configured_widget_token())
    accepted = [token for token in accepted if token]
    if not accepted:
        return False

    header = (request.headers.get("Authorization") or "").strip()
    if not header.lower().startswith("bearer "):
        return False
    supplied = header[7:].strip()
    return bool(supplied) and any(secrets.compare_digest(supplied, token) for token in accepted)


def _auth_error(*, allow_widget: bool = False) -> web.Response:
    configured = bool(_configured_token()) or (allow_widget and bool(_configured_widget_token()))
    if not configured:
        return web.json_response(
            {"ok": False, "error": "companion_not_configured"},
            status=503,
        )
    return web.json_response({"ok": False, "error": "unauthorized"}, status=401)


def _utc_naive(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None)


async def _calendar_snapshot_with_budget(
    task: asyncio.Task,
    *,
    timeout_sec: float = 1.0,
) -> TodayCalendarSnapshot:
    """Keep /today responsive even when CalDAV is cold.

    The unfinished task is deliberately left running so it can populate the
    short calendar cache for the next app/widget refresh.
    """
    done, _pending = await asyncio.wait({task}, timeout=max(0.0, float(timeout_sec)))
    if task in done:
        try:
            return task.result()
        except Exception:
            return TodayCalendarSnapshot(events=(), unavailable=True)

    def _consume_result(completed: asyncio.Task) -> None:
        try:
            completed.result()
        except Exception:
            pass

    task.add_done_callback(_consume_result)
    return TodayCalendarSnapshot(events=(), unavailable=False, pending=True)


def _parse_client_datetime(value: object, *, tz: ZoneInfo) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(timezone.utc)


def _active_dismissed_event_items(
    state: dict | None,
    *,
    now_utc: datetime,
) -> list[dict[str, str]]:
    payload = (state or {}).get("payload") or {}
    raw_items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(raw_items, list):
        return []

    active: list[dict[str, str]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        event_id = str(item.get("id") or "").strip()
        until_raw = str(item.get("until") or "").strip()
        if not event_id or not until_raw:
            continue
        try:
            until = datetime.fromisoformat(until_raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        until = until.astimezone(timezone.utc)
        if until > now_utc:
            active.append({"id": event_id, "until": until.isoformat()})
    return active


def _utc_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _focus_started_at(state: dict | None, task_id: int | None) -> str | None:
    if not state or task_id is None:
        return None
    payload = state.get("payload") or {}
    try:
        focused_id = int(payload.get("task_id"))
    except (TypeError, ValueError):
        return None
    if focused_id != int(task_id):
        return None
    value = payload.get("started_at")
    return str(value).strip() if value else None


def _focus_previous_status(state: dict | None, task_id: int | None) -> str | None:
    if not state or task_id is None:
        return None
    payload = state.get("payload") or {}
    try:
        focused_id = int(payload.get("task_id"))
    except (TypeError, ValueError):
        return None
    if focused_id != int(task_id):
        return None
    value = str(payload.get("previous_status") or "").strip().lower()
    if not value or value in {"in_progress", "done", "postponed"}:
        return None
    return value


def _clean_task_steps(payload: object) -> list[str]:
    if not isinstance(payload, dict):
        return []
    raw = payload.get("steps")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        single = payload.get("step")
        raw = [single] if isinstance(single, str) else []

    steps: list[str] = []
    for item in raw:
        text = str(item or "").strip()
        if not text:
            continue
        text = " ".join(text.split())
        if len(text) > 160:
            text = text[:157].rstrip() + "…"
        if text not in steps:
            steps.append(text)
        if len(steps) >= 3:
            break
    return steps


def _attention_sort_key(
    row: dict,
    now_utc: datetime,
    end_utc_aware: datetime,
    *,
    focus_task_id: int | None = None,
) -> tuple:
    deadline = _utc_aware(row.get("deadline"))
    created_at = _utc_aware(row.get("created_at"))
    status = str(row.get("status") or "").lower()
    task_id = int(row.get("id") or 0)

    if focus_task_id is not None and task_id == int(focus_task_id):
        bucket = -1
        time_key = deadline.timestamp() if deadline else 0.0
    elif status == "in_progress":
        bucket = 0
        time_key = deadline.timestamp() if deadline else float("inf")
    elif deadline and now_utc <= deadline < end_utc_aware:
        bucket = 1
        time_key = deadline.timestamp()
    elif deadline and deadline < now_utc:
        bucket = 2
        # Recent overdue work is more actionable than ancient backlog debt.
        time_key = -deadline.timestamp()
    elif deadline is None:
        bucket = 3
        time_key = created_at.timestamp() if created_at else 0.0
    else:
        bucket = 4
        time_key = deadline.timestamp()

    return (bucket, time_key, task_id)


def _select_attention_tasks(
    rows,
    *,
    now_utc: datetime,
    end_utc_aware: datetime,
    limit: int = ATTENTION_TASK_LIMIT,
    urgent_limit: int = ATTENTION_URGENT_LIMIT,
    focus_task_id: int | None = None,
) -> list[dict]:
    """Keep the daily surface useful without letting overdue work consume it."""
    ordered = sorted(
        (dict(row) for row in rows or []),
        key=lambda row: _attention_sort_key(
            row,
            now_utc,
            end_utc_aware,
            focus_task_id=focus_task_id,
        ),
    )
    selected: list[dict] = []
    deferred_urgent: list[dict] = []
    urgent_count = 0

    for row in ordered:
        deadline = _utc_aware(row.get("deadline"))
        is_urgent = bool(deadline and deadline < end_utc_aware)
        if is_urgent and urgent_count >= urgent_limit:
            deferred_urgent.append(row)
            continue

        selected.append(row)
        if is_urgent:
            urgent_count += 1
        if len(selected) >= limit:
            return selected

    if len(selected) < limit:
        selected.extend(deferred_urgent[: limit - len(selected)])
    return selected


def attach_companion_routes(app: web.Application, ctx) -> None:
    """Attach canonical Assistant API plus legacy companion aliases."""

    async def _today(request: web.Request) -> web.StreamResponse:
        return await handle_today(request, ctx)

    async def _tasks(request: web.Request) -> web.StreamResponse:
        return await handle_tasks(request, ctx)

    async def _projects(request: web.Request) -> web.StreamResponse:
        return await handle_projects(request, ctx)

    async def _ideas(request: web.Request) -> web.StreamResponse:
        return await handle_ideas(request, ctx)

    async def _idea_promote(request: web.Request) -> web.StreamResponse:
        return await handle_idea_promote(request, ctx)

    async def _idea_archive(request: web.Request) -> web.StreamResponse:
        return await handle_idea_archive(request, ctx)

    async def _task_update(request: web.Request) -> web.StreamResponse:
        return await handle_task_update(request, ctx)

    async def _capture(request: web.Request) -> web.StreamResponse:
        return await handle_capture(request, ctx)

    async def _intake(request: web.Request) -> web.StreamResponse:
        return await handle_intake(request, ctx)

    async def _voice_intake(request: web.Request) -> web.StreamResponse:
        return await handle_voice_intake(request, ctx)

    async def _intake_pending(request: web.Request) -> web.StreamResponse:
        return await handle_intake_pending(request, ctx)

    async def _intake_confirm(request: web.Request) -> web.StreamResponse:
        return await handle_intake_confirm(request, ctx)

    async def _intake_cancel(request: web.Request) -> web.StreamResponse:
        return await handle_intake_cancel(request, ctx)

    async def _focus(request: web.Request) -> web.StreamResponse:
        return await handle_task_focus(request, ctx)

    async def _unfocus(request: web.Request) -> web.StreamResponse:
        return await handle_task_unfocus(request, ctx)

    async def _steps(request: web.Request) -> web.StreamResponse:
        return await handle_task_steps(request, ctx)

    async def _done(request: web.Request) -> web.StreamResponse:
        return await handle_task_done(request, ctx)

    async def _dismiss_event(request: web.Request) -> web.StreamResponse:
        return await handle_event_dismiss(request, ctx)

    async def _snooze_reminder(request: web.Request) -> web.StreamResponse:
        return await handle_reminder_snooze(request, ctx)

    # Canonical client API.
    app.router.add_get("/api/v1/today", _today)
    app.router.add_get("/api/v1/tasks", _tasks)
    app.router.add_get("/api/v1/projects", _projects)
    app.router.add_get("/api/v1/ideas", _ideas)
    app.router.add_post("/api/v1/ideas/{idea_id}/promote", _idea_promote)
    app.router.add_post("/api/v1/ideas/{idea_id}/archive", _idea_archive)
    app.router.add_patch("/api/v1/tasks/{task_id}", _task_update)
    app.router.add_post("/api/v1/capture", _capture)
    app.router.add_post("/api/v1/intake", _intake)
    app.router.add_post("/api/v1/intake/audio", _voice_intake)
    app.router.add_get("/api/v1/intake/pending", _intake_pending)
    app.router.add_post("/api/v1/intake/{pending_action_id}/confirm", _intake_confirm)
    app.router.add_post("/api/v1/intake/{pending_action_id}/cancel", _intake_cancel)
    app.router.add_post("/api/v1/tasks/{task_id}/focus", _focus)
    app.router.add_post("/api/v1/tasks/{task_id}/unfocus", _unfocus)
    app.router.add_post("/api/v1/tasks/{task_id}/steps", _steps)
    app.router.add_post("/api/v1/tasks/{task_id}/done", _done)
    app.router.add_post("/api/v1/attention/dismiss-event", _dismiss_event)
    app.router.add_post("/api/v1/reminders/{reminder_id}/snooze", _snooze_reminder)

    # Compatibility for already installed companion builds.
    app.router.add_get("/api/v1/companion/today", _today)
    app.router.add_post("/api/v1/companion/capture", _capture)
    app.router.add_post("/api/v1/companion/intake/audio", _voice_intake)
    app.router.add_post("/api/v1/companion/tasks/{task_id}/done", _done)


async def handle_today(request: web.Request, ctx) -> web.StreamResponse:
    if not _authorized(request, allow_widget=True):
        return _auth_error(allow_widget=True)

    pool: asyncpg.Pool | None = ctx.deps.db_pool
    if not pool:
        return web.json_response({"ok": False, "error": "db_unavailable"}, status=503)

    tz_name = resolve_tz_name(ctx.deps.tz_name)
    tz = ZoneInfo(tz_name)
    now_local = datetime.now(tz)
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)
    start_utc = _utc_naive(start_local)
    end_utc = _utc_naive(end_local)
    now_utc = datetime.now(timezone.utc)

    work_calendar_url = (os.getenv("ICLOUD_CALENDAR_URL_WORK") or "").strip()
    personal_calendar_url = (os.getenv("ICLOUD_CALENDAR_URL_PERSONAL") or "").strip()
    bitrix_calendar_url = (os.getenv("ICLOUD_CALENDAR_URL_BITRIX") or "").strip()
    calendar_task = asyncio.create_task(
        fetch_today_calendar(
            tz=tz,
            calendar_urls=[work_calendar_url, personal_calendar_url, bitrix_calendar_url],
            icloud=getattr(ctx.deps, "icloud", None),
        )
    )

    focus_task_id: int | None = None
    focus_started_at: str | None = None
    async with pool.acquire() as conn:
        focus_state = await get_conversation_state(
            conn,
            int(ctx.deps.admin_id or 0),
            "attention_focus",
        )
        if focus_state:
            try:
                focus_task_id = int((focus_state.get("payload") or {}).get("task_id"))
            except (TypeError, ValueError):
                focus_task_id = None
        focus_started_at = _focus_started_at(focus_state, focus_task_id)

        dismissed_event_state = await get_conversation_state(
            conn,
            int(ctx.deps.admin_id or 0),
            ATTENTION_DISMISSED_EVENTS_FLOW,
        )
        dismissed_event_ids = {
            item["id"]
            for item in _active_dismissed_event_items(
                dismissed_event_state,
                now_utc=now_utc,
            )
        }

        task_rows = await conn.fetch(
            """
            SELECT t.id, t.title, t.deadline, t.status, t.kind, t.created_at,
                   p.code AS project_code, COALESCE(tm.name, '') AS assignee
            FROM tasks t
            JOIN projects p ON p.id=t.project_id
            LEFT JOIN team tm ON tm.id=t.assignee_id
            WHERE t.status NOT IN ('done', 'postponed')
              AND t.kind != 'super'
              AND p.status IN ('active', 'system')
            ORDER BY t.created_at ASC, t.id ASC
            LIMIT 200
            """
        )
        reminder_rows = await conn.fetch(
            """
            SELECT id, text, remind_at
            FROM reminders
            WHERE chat_id=$1
              AND COALESCE(status, 'pending') IN ('pending', 'retry')
              AND COALESCE(is_sent, FALSE)=FALSE
              AND remind_at < $3
            ORDER BY
              CASE WHEN remind_at <= $2 THEN 0 ELSE 1 END,
              CASE WHEN remind_at <= $2 THEN remind_at END DESC,
              remind_at ASC,
              id ASC
            LIMIT 50
            """,
            int(ctx.deps.admin_id or 0),
            _utc_naive(now_utc),
            end_utc,
        )

    selected_task_rows = _select_attention_tasks(
        task_rows,
        now_utc=now_utc,
        end_utc_aware=end_local.astimezone(timezone.utc),
        focus_task_id=focus_task_id,
    )

    tasks = []
    for row in selected_task_rows:
        deadline_utc = _utc_aware(row["deadline"])
        deadline_local = deadline_utc.astimezone(tz) if deadline_utc else None
        tasks.append(
            {
                "id": int(row["id"]),
                "title": str(row["title"] or ""),
                "project": "" if str(row["kind"] or "task") == "personal" else str(row["project_code"] or ""),
                "kind": str(row["kind"] or "task"),
                "assignee": str(row["assignee"] or ""),
                "status": str(row["status"] or "todo"),
                "deadline": deadline_local.isoformat() if deadline_local else None,
                "overdue": bool(deadline_utc and deadline_utc < now_utc),
                "focused": focus_task_id is not None and int(row["id"]) == focus_task_id,
                "focused_since": (
                    focus_started_at
                    if focus_task_id is not None and int(row["id"]) == focus_task_id
                    else None
                ),
            }
        )

    reminders = []
    for row in reminder_rows:
        remind_utc = _utc_aware(row["remind_at"])
        remind_local = remind_utc.astimezone(tz) if remind_utc else None
        reminders.append(
            {
                "id": int(row["id"]),
                "text": str(row["text"] or ""),
                "at": remind_local.isoformat() if remind_local else None,
            }
        )

    calendar_snapshot = await _calendar_snapshot_with_budget(calendar_task)
    events = []
    for event in calendar_snapshot.events:
        event_start_utc = _utc_aware(event.dtstart_utc)
        event_end_utc = _utc_aware(event.dtend_utc)
        if event_start_utc is None or event_end_utc is None or event_end_utc <= now_utc:
            continue

        start_local_event = event_start_utc.astimezone(tz)
        end_local_event = event_end_utc.astimezone(tz)
        duration_sec = max(0.0, (event_end_utc - event_start_utc).total_seconds())
        looks_all_day = (
            duration_sec >= 23 * 3600
            and start_local_event.hour == 0
            and start_local_event.minute == 0
            and end_local_event.hour == 0
            and end_local_event.minute == 0
        )
        if looks_all_day:
            continue

        calendar_url = str(event.calendar_url or "")
        if work_calendar_url and calendar_url.rstrip("/") == work_calendar_url.rstrip("/"):
            calendar_kind = "work"
        elif bitrix_calendar_url and calendar_url.rstrip("/") == bitrix_calendar_url.rstrip("/"):
            calendar_kind = "work"
        elif personal_calendar_url and calendar_url.rstrip("/") == personal_calendar_url.rstrip("/"):
            calendar_kind = "personal"
        else:
            calendar_kind = "calendar"

        event_id = str(event.uid or "").strip()
        if not event_id:
            event_id = f"{calendar_kind}:{int(event_start_utc.timestamp())}:{str(event.summary or '')[:80]}"

        if event_id in dismissed_event_ids:
            continue

        events.append(
            {
                "id": event_id,
                "title": str(event.summary or "Без названия"),
                "start": start_local_event.isoformat(),
                "end": end_local_event.isoformat(),
                "kind": calendar_kind,
            }
        )
        if len(events) >= 8:
            break

    return web.json_response(
        {
            "ok": True,
            "date": start_local.date().isoformat(),
            "timezone": tz_name,
            "tasks": tasks,
            "reminders": reminders,
            "events": events,
            "calendar_unavailable": bool(calendar_snapshot.unavailable),
            "calendar_pending": bool(calendar_snapshot.pending),
        }
    )


async def handle_reminder_snooze(request: web.Request, ctx) -> web.StreamResponse:
    if not _authorized(request, allow_widget=True):
        return _auth_error(allow_widget=True)

    pool: asyncpg.Pool | None = ctx.deps.db_pool
    if not pool:
        return web.json_response({"ok": False, "error": "db_unavailable"}, status=503)

    try:
        reminder_id = int(request.match_info["reminder_id"])
    except Exception:
        return web.json_response({"ok": False, "error": "invalid_reminder_id"}, status=400)

    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    try:
        minutes = int(payload.get("minutes", 15))
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "invalid_minutes"}, status=400)
    if minutes != 15:
        return web.json_response({"ok": False, "error": "unsupported_minutes"}, status=400)

    delivery = None
    async with pool.acquire() as conn:
        delivery = await conn.fetchrow(
            """
            SELECT text, chat_id, telegram_message_id
            FROM reminders
            WHERE id=$1
            """,
            reminder_id,
        )
        result = await snooze_reminder(
            conn,
            reminder_id=reminder_id,
            minutes=minutes,
            fallback_chat_id=int(ctx.deps.admin_id or 0),
            tz_name=ctx.deps.tz_name,
            store_tz=bool(getattr(ctx.deps, "db_reminders_remind_at_timestamptz", False)),
        )

    if result is None:
        return web.json_response({"ok": False, "error": "reminder_not_found"}, status=404)

    new_id, new_time, _label = result

    if delivery and delivery["telegram_message_id"]:
        await mark_telegram_reminder_snoozed(
            bot=ctx.bot,
            chat_id=int(delivery["chat_id"] or ctx.deps.admin_id or 0),
            message_id=int(delivery["telegram_message_id"]),
            text=str(delivery["text"] or ""),
            label="15 минут",
        )

    return web.json_response(
        {
            "ok": True,
            "status": "snoozed",
            "reminder_id": reminder_id,
            "new_reminder_id": new_id,
            "at": new_time.isoformat(),
            "minutes": minutes,
        }
    )


async def handle_event_dismiss(request: web.Request, ctx) -> web.StreamResponse:
    """Hide a calendar event from attention surfaces without deleting it."""
    if not _authorized(request, allow_widget=True):
        return _auth_error(allow_widget=True)

    pool: asyncpg.Pool | None = ctx.deps.db_pool
    if not pool:
        return web.json_response({"ok": False, "error": "db_unavailable"}, status=503)

    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid_json"}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"ok": False, "error": "invalid_json"}, status=400)

    event_id = str(payload.get("event_id") or "").strip()
    if not event_id or len(event_id) > 1024:
        return web.json_response({"ok": False, "error": "invalid_event_id"}, status=400)

    tz = ZoneInfo(resolve_tz_name(ctx.deps.tz_name))
    now_utc = datetime.now(timezone.utc)
    until_utc = _parse_client_datetime(payload.get("until"), tz=tz)
    if until_utc is None:
        return web.json_response({"ok": False, "error": "invalid_until"}, status=400)

    # Dismissal is an attention preference, not a permanent calendar mutation.
    # Bound stale/bogus client values so one tap cannot hide a UID indefinitely.
    until_utc = min(until_utc, now_utc + timedelta(days=2))
    if until_utc <= now_utc:
        return web.json_response({"ok": True, "status": "expired"})

    chat_id = int(ctx.deps.admin_id or 0)
    async with pool.acquire() as conn:
        state = await get_conversation_state(
            conn,
            chat_id,
            ATTENTION_DISMISSED_EVENTS_FLOW,
        )
        items = _active_dismissed_event_items(state, now_utc=now_utc)
        items = [item for item in items if item["id"] != event_id]
        items.append({"id": event_id, "until": until_utc.isoformat()})
        items = items[-50:]

        await set_conversation_state(
            conn,
            chat_id,
            ATTENTION_DISMISSED_EVENTS_FLOW,
            step="active",
            payload={"items": items},
            ttl_sec=2 * 24 * 3600,
        )

    return web.json_response(
        {
            "ok": True,
            "status": "dismissed",
            "event_id": event_id,
            "until": until_utc.isoformat(),
        }
    )


async def handle_tasks(request: web.Request, ctx) -> web.StreamResponse:
    """Return the active task backlog for second-level native browsing."""
    if not _authorized(request):
        return _auth_error()

    pool: asyncpg.Pool | None = ctx.deps.db_pool
    if not pool:
        return web.json_response({"ok": False, "error": "db_unavailable"}, status=503)

    try:
        limit = max(1, min(200, int(request.query.get("limit", "100"))))
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "invalid_limit"}, status=400)

    tz_name = resolve_tz_name(ctx.deps.tz_name)
    tz = ZoneInfo(tz_name)
    now_local = datetime.now(tz)
    end_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    now_utc = datetime.now(timezone.utc)

    focus_task_id: int | None = None
    focus_started_at: str | None = None
    async with pool.acquire() as conn:
        focus_state = await get_conversation_state(
            conn,
            int(ctx.deps.admin_id or 0),
            "attention_focus",
        )
        if focus_state:
            try:
                focus_task_id = int((focus_state.get("payload") or {}).get("task_id"))
            except (TypeError, ValueError):
                focus_task_id = None
        focus_started_at = _focus_started_at(focus_state, focus_task_id)

        rows = await conn.fetch(
            """
            SELECT t.id, t.title, t.deadline, t.status, t.kind, t.created_at,
                   p.code AS project_code, COALESCE(tm.name, '') AS assignee
            FROM tasks t
            JOIN projects p ON p.id=t.project_id
            LEFT JOIN team tm ON tm.id=t.assignee_id
            WHERE t.status NOT IN ('done', 'postponed')
              AND t.kind != 'super'
              AND p.status IN ('active', 'system')
            ORDER BY t.created_at ASC, t.id ASC
            LIMIT 500
            """
        )

    ordered = sorted(
        (dict(row) for row in rows),
        key=lambda row: _attention_sort_key(
            row,
            now_utc,
            end_local.astimezone(timezone.utc),
            focus_task_id=focus_task_id,
        ),
    )

    tasks = []
    for row in ordered[:limit]:
        deadline_utc = _utc_aware(row["deadline"])
        deadline_local = deadline_utc.astimezone(tz) if deadline_utc else None
        tasks.append(
            {
                "id": int(row["id"]),
                "title": str(row["title"] or ""),
                "project": "" if str(row["kind"] or "task") == "personal" else str(row["project_code"] or ""),
                "kind": str(row["kind"] or "task"),
                "assignee": str(row["assignee"] or ""),
                "status": str(row["status"] or "todo"),
                "deadline": deadline_local.isoformat() if deadline_local else None,
                "overdue": bool(deadline_utc and deadline_utc < now_utc),
                "focused": focus_task_id is not None and int(row["id"]) == focus_task_id,
                "focused_since": (
                    focus_started_at
                    if focus_task_id is not None and int(row["id"]) == focus_task_id
                    else None
                ),
            }
        )

    return web.json_response(
        {
            "ok": True,
            "timezone": tz_name,
            "tasks": tasks,
        }
    )


async def handle_ideas(request: web.Request, ctx) -> web.StreamResponse:
    if not _authorized(request):
        return _auth_error()

    pool: asyncpg.Pool | None = ctx.deps.db_pool
    if not pool:
        return web.json_response({"ok": False, "error": "db_unavailable"}, status=503)

    try:
        limit = max(1, min(200, int(request.query.get("limit", "100"))))
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "invalid_limit"}, status=400)

    async with pool.acquire() as conn:
        _total, rows = await list_active_ideas(
            conn,
            chat_id=int(ctx.deps.admin_id or 0),
            limit=limit,
            offset=0,
        )

    return web.json_response(
        {
            "ok": True,
            "ideas": [
                {
                    "id": int(row["id"]),
                    "text": str(row["text"] or ""),
                    "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                }
                for row in rows
            ],
        }
    )


async def handle_idea_promote(request: web.Request, ctx) -> web.StreamResponse:
    if not _authorized(request):
        return _auth_error()

    pool: asyncpg.Pool | None = ctx.deps.db_pool
    if not pool:
        return web.json_response({"ok": False, "error": "db_unavailable"}, status=503)

    try:
        idea_id = int(request.match_info["idea_id"])
    except Exception:
        return web.json_response({"ok": False, "error": "invalid_idea_id"}, status=400)

    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await promote_idea(
                conn,
                chat_id=int(ctx.deps.admin_id or 0),
                idea_id=idea_id,
            )

    if result is None:
        return web.json_response({"ok": False, "error": "idea_not_active"}, status=404)

    task_id, _text = result
    return web.json_response(
        {"ok": True, "idea_id": idea_id, "task_id": int(task_id), "status": "promoted"}
    )


async def handle_idea_archive(request: web.Request, ctx) -> web.StreamResponse:
    if not _authorized(request):
        return _auth_error()

    pool: asyncpg.Pool | None = ctx.deps.db_pool
    if not pool:
        return web.json_response({"ok": False, "error": "db_unavailable"}, status=503)

    try:
        idea_id = int(request.match_info["idea_id"])
    except Exception:
        return web.json_response({"ok": False, "error": "invalid_idea_id"}, status=400)

    async with pool.acquire() as conn:
        async with conn.transaction():
            text_value = await archive_idea(
                conn,
                chat_id=int(ctx.deps.admin_id or 0),
                idea_id=idea_id,
            )

    if text_value is None:
        return web.json_response({"ok": False, "error": "idea_not_active"}, status=404)

    return web.json_response(
        {"ok": True, "idea_id": idea_id, "status": "archived"}
    )


async def handle_projects(request: web.Request, ctx) -> web.StreamResponse:
    if not _authorized(request):
        return _auth_error()

    pool: asyncpg.Pool | None = ctx.deps.db_pool
    if not pool:
        return web.json_response({"ok": False, "error": "db_unavailable"}, status=503)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, code, name
            FROM projects
            WHERE status='active'
            ORDER BY CASE WHEN UPPER(code)='INBOX' THEN 0 ELSE 1 END, code
            """
        )

    return web.json_response(
        {
            "ok": True,
            "projects": [
                {
                    "id": int(row["id"]),
                    "code": str(row["code"] or ""),
                    "name": str(row["name"] or ""),
                }
                for row in rows
            ],
        }
    )


async def handle_task_update(request: web.Request, ctx) -> web.StreamResponse:
    if not _authorized(request):
        return _auth_error()

    pool: asyncpg.Pool | None = ctx.deps.db_pool
    if not pool:
        return web.json_response({"ok": False, "error": "db_unavailable"}, status=503)

    try:
        task_id = int(request.match_info["task_id"])
    except Exception:
        return web.json_response({"ok": False, "error": "invalid_task_id"}, status=400)

    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid_json"}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"ok": False, "error": "invalid_json"}, status=400)

    allowed = {"title", "project_code", "deadline"}
    if not any(key in payload for key in allowed):
        return web.json_response({"ok": False, "error": "nothing_to_update"}, status=400)

    title = None
    if "title" in payload:
        title = str(payload.get("title") or "").strip()
        if not title:
            return web.json_response({"ok": False, "error": "empty_title"}, status=400)
        if len(title) > 1000:
            return web.json_response({"ok": False, "error": "title_too_long"}, status=413)

    tz_name = resolve_tz_name(ctx.deps.tz_name)
    deadline_db = None
    if "deadline" in payload and payload.get("deadline") is not None:
        raw_deadline = str(payload.get("deadline") or "").strip()
        try:
            parsed = datetime.fromisoformat(raw_deadline.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=ZoneInfo(tz_name))
            deadline_db = to_db_utc(
                parsed,
                tz_name=tz_name,
                store_tz=bool(getattr(ctx.deps, "db_tasks_deadline_timestamptz", False)),
            )
        except Exception:
            return web.json_response({"ok": False, "error": "invalid_deadline"}, status=400)

    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT t.id, t.title, t.status, t.kind, t.project_id,
                       p.code AS project_code
                FROM tasks t
                JOIN projects p ON p.id=t.project_id
                WHERE t.id=$1
                FOR UPDATE
                """,
                task_id,
            )
            if not row:
                return web.json_response({"ok": False, "error": "task_not_found"}, status=404)
            if str(row["kind"] or "task").lower() == "super":
                return web.json_response({"ok": False, "error": "super_task_not_supported"}, status=409)
            if str(row["status"] or "").lower() == "done":
                return web.json_response({"ok": False, "error": "task_not_active"}, status=409)

            project_id = int(row["project_id"])
            project_code = str(row["project_code"] or "")
            if "project_code" in payload and str(row["kind"] or "task") == "personal":
                return web.json_response({"ok": False, "error": "personal_task_has_no_project"}, status=409)

            if "project_code" in payload:
                requested = str(payload.get("project_code") or "").strip()
                if not requested:
                    return web.json_response({"ok": False, "error": "empty_project"}, status=400)
                project = await conn.fetchrow(
                    """
                    SELECT id, code
                    FROM projects
                    WHERE status='active' AND UPPER(code)=UPPER($1)
                    LIMIT 1
                    """,
                    requested,
                )
                if not project:
                    return web.json_response({"ok": False, "error": "project_not_found"}, status=404)
                project_id = int(project["id"])
                project_code = str(project["code"] or "")

            if title is not None:
                await conn.execute("UPDATE tasks SET title=$2, updated_at=NOW() WHERE id=$1", task_id, title)
            if "project_code" in payload:
                await conn.execute("UPDATE tasks SET project_id=$2, updated_at=NOW() WHERE id=$1", task_id, project_id)
            if "deadline" in payload:
                await conn.execute("UPDATE tasks SET deadline=$2, updated_at=NOW() WHERE id=$1", task_id, deadline_db)

            effective_title = title if title is not None else str(row["title"] or "")
            await db_add_event(
                conn,
                "task_updated",
                project_id,
                task_id,
                f"iOS edit: [{project_code}] #{task_id} {effective_title}",
            )

    return web.json_response({"ok": True, "task_id": task_id, "status": "updated"})


async def handle_voice_intake(request: web.Request, ctx) -> web.StreamResponse:
    if not _authorized(request):
        return _auth_error()

    pool: asyncpg.Pool | None = ctx.deps.db_pool
    if not pool:
        return web.json_response({"ok": False, "error": "db_unavailable"}, status=503)

    llm = getattr(ctx.deps, "llm", None)
    if llm is None or not getattr(llm, "enabled", False):
        return web.json_response({"ok": False, "error": "voice_unavailable"}, status=503)

    if not request.content_type.startswith("multipart/"):
        return web.json_response({"ok": False, "error": "multipart_required"}, status=400)

    try:
        reader = await request.multipart()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid_multipart"}, status=400)

    audio = bytearray()
    filename = "voice.m4a"
    mime_type = "audio/mp4"
    context: str | None = None
    client_id: str | None = None

    async for part in reader:
        name = str(part.name or "")
        if name == "audio":
            filename = str(part.filename or filename)
            mime_type = str(part.headers.get("Content-Type") or mime_type)
            while True:
                chunk = await part.read_chunk(size=64 * 1024)
                if not chunk:
                    break
                audio.extend(chunk)
                if len(audio) > MAX_VOICE_BYTES:
                    return web.json_response(
                        {"ok": False, "error": "audio_too_large", "max_bytes": MAX_VOICE_BYTES},
                        status=413,
                    )
        elif name == "context":
            context = (await part.text()).strip() or None
        elif name == "client_id":
            client_id = (await part.text()).strip() or None

    if not audio:
        return web.json_response({"ok": False, "error": "empty_audio"}, status=400)

    if client_id is not None:
        if len(client_id) > 64 or not all(ch.isalnum() or ch in "-_" for ch in client_id):
            return web.json_response({"ok": False, "error": "invalid_client_id"}, status=400)

    if context is not None and len(context) > MAX_CAPTURE_LEN * 4:
        return web.json_response({"ok": False, "error": "context_too_long"}, status=413)

    try:
        transcript = await llm.transcribe_audio(
            audio_bytes=bytes(audio),
            filename=filename,
            mime_type=mime_type,
        )
    except Exception:
        return web.json_response({"ok": False, "error": "transcription_failed"}, status=502)

    transcript = str(transcript or "").strip()
    if not transcript:
        return web.json_response({"ok": False, "error": "empty_transcript"}, status=422)
    if len(transcript) > MAX_CAPTURE_LEN:
        transcript = transcript[:MAX_CAPTURE_LEN]

    result = await process_native_capture(
        text=transcript,
        deps=ctx.deps,
        db_pool=pool,
        chat_id=int(ctx.deps.admin_id or 0),
        prepend_text=context,
        source="ios_voice",
        capture_id=client_id,
    )
    result["transcript"] = transcript
    return web.json_response(result, status=200 if result.get("ok") else 400)


async def handle_intake(request: web.Request, ctx) -> web.StreamResponse:
    if not _authorized(request):
        return _auth_error()

    pool: asyncpg.Pool | None = ctx.deps.db_pool
    if not pool:
        return web.json_response({"ok": False, "error": "db_unavailable"}, status=503)

    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid_json"}, status=400)

    text = str((payload or {}).get("text") or "").strip()
    if not text:
        return web.json_response({"ok": False, "error": "empty_text"}, status=400)
    if len(text) > MAX_CAPTURE_LEN:
        return web.json_response(
            {"ok": False, "error": "text_too_long", "max_length": MAX_CAPTURE_LEN},
            status=413,
        )

    context = str((payload or {}).get("context") or "").strip() or None
    client_id = str((payload or {}).get("client_id") or "").strip() or None
    if client_id is not None:
        if len(client_id) > 64 or not all(ch.isalnum() or ch in "-_" for ch in client_id):
            return web.json_response({"ok": False, "error": "invalid_client_id"}, status=400)

    result = await process_native_capture(
        text=text,
        deps=ctx.deps,
        db_pool=pool,
        chat_id=int(ctx.deps.admin_id or 0),
        prepend_text=context,
        source="ios",
        capture_id=client_id,
    )
    return web.json_response(result, status=200 if result.get("ok") else 400)


async def handle_intake_pending(request: web.Request, ctx) -> web.StreamResponse:
    if not _authorized(request):
        return _auth_error()

    pool: asyncpg.Pool | None = ctx.deps.db_pool
    if not pool:
        return web.json_response({"ok": False, "error": "db_unavailable"}, status=503)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, kind, payload_json
            FROM pending_actions
            WHERE chat_id=$1
              AND status='pending'
              AND expires_at > NOW()
              AND COALESCE(payload_json->>'source', '') LIKE 'ios%'
            ORDER BY created_at ASC, id ASC
            LIMIT 20
            """,
            int(ctx.deps.admin_id or 0),
        )

    pending = []
    for row in rows:
        payload = row["payload_json"] or {}
        if isinstance(payload, str):
            try:
                import json
                payload = json.loads(payload)
            except Exception:
                payload = {}
        kind = str(row["kind"] or "")
        title = str(
            payload.get("summary")
            or payload.get("title")
            or payload.get("reminder_text")
            or payload.get("idea_text")
            or "Запись"
        )
        pending.append(
            {
                "status": "needs_confirmation",
                "kind": kind,
                "title": title,
                "pending_action_id": int(row["id"]),
                "payload": payload,
            }
        )

    return web.json_response({"ok": True, "pending": pending})


async def handle_intake_confirm(request: web.Request, ctx) -> web.StreamResponse:
    if not _authorized(request):
        return _auth_error()

    pool: asyncpg.Pool | None = ctx.deps.db_pool
    if not pool:
        return web.json_response({"ok": False, "error": "db_unavailable"}, status=503)
    try:
        pending_action_id = int(request.match_info["pending_action_id"])
    except Exception:
        return web.json_response({"ok": False, "error": "invalid_pending_action_id"}, status=400)

    result = await confirm_native_action(
        pending_action_id=pending_action_id,
        deps=ctx.deps,
        db_pool=pool,
        chat_id=int(ctx.deps.admin_id or 0),
    )
    return web.json_response(result, status=200 if result.get("ok") else 409)


async def handle_intake_cancel(request: web.Request, ctx) -> web.StreamResponse:
    if not _authorized(request):
        return _auth_error()

    pool: asyncpg.Pool | None = ctx.deps.db_pool
    if not pool:
        return web.json_response({"ok": False, "error": "db_unavailable"}, status=503)
    try:
        pending_action_id = int(request.match_info["pending_action_id"])
    except Exception:
        return web.json_response({"ok": False, "error": "invalid_pending_action_id"}, status=400)

    result = await cancel_native_action(
        pending_action_id=pending_action_id,
        db_pool=pool,
        chat_id=int(ctx.deps.admin_id or 0),
    )
    return web.json_response(result, status=200 if result.get("ok") else 409)


async def handle_capture(request: web.Request, ctx) -> web.StreamResponse:
    if not _authorized(request):
        return _auth_error()

    pool: asyncpg.Pool | None = ctx.deps.db_pool
    if not pool:
        return web.json_response({"ok": False, "error": "db_unavailable"}, status=503)

    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid_json"}, status=400)

    text = str((payload or {}).get("text") or "").strip()
    if not text:
        return web.json_response({"ok": False, "error": "empty_text"}, status=400)
    if len(text) > MAX_CAPTURE_LEN:
        return web.json_response(
            {"ok": False, "error": "text_too_long", "max_length": MAX_CAPTURE_LEN},
            status=413,
        )

    async with pool.acquire() as conn:
        async with conn.transaction():
            project_id = await ensure_inbox_project_id(conn)
            task_id = int(
                await conn.fetchval(
                    """
                    INSERT INTO tasks (project_id, title, status, kind)
                    VALUES ($1, $2, 'todo', 'task')
                    RETURNING id
                    """,
                    int(project_id),
                    text,
                )
            )
            await db_add_event(
                conn,
                "task_created",
                int(project_id),
                task_id,
                f"iOS capture: #{task_id} {text}",
            )

    return web.json_response(
        {
            "ok": True,
            "task": {
                "id": task_id,
                "title": text,
                "project": "INBOX",
            },
        },
        status=201,
    )


async def handle_task_focus(request: web.Request, ctx) -> web.StreamResponse:
    if not _authorized(request, allow_widget=True):
        return _auth_error(allow_widget=True)

    pool: asyncpg.Pool | None = ctx.deps.db_pool
    if not pool:
        return web.json_response({"ok": False, "error": "db_unavailable"}, status=503)

    try:
        task_id = int(request.match_info["task_id"])
    except Exception:
        return web.json_response({"ok": False, "error": "invalid_task_id"}, status=400)

    async with pool.acquire() as conn:
        async with conn.transaction():
            focus_state = await get_conversation_state(
                conn,
                int(ctx.deps.admin_id or 0),
                "attention_focus",
            )
            payload = (focus_state or {}).get("payload") or {}
            try:
                previous_focus_id = int(payload.get("task_id"))
            except (TypeError, ValueError):
                previous_focus_id = None

            if previous_focus_id is not None and previous_focus_id != task_id:
                previous_status = _focus_previous_status(focus_state, previous_focus_id)
                if previous_status:
                    await conn.execute(
                        """
                        UPDATE tasks
                        SET status=$2, updated_at=NOW()
                        WHERE id=$1 AND status='in_progress'
                        """,
                        previous_focus_id,
                        previous_status,
                    )

            started_at = _focus_started_at(focus_state, task_id)
            if started_at is None:
                started_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

            row = await conn.fetchrow(
                """
                SELECT t.id, t.title, t.status, t.kind, t.project_id, p.code AS project_code
                FROM tasks t
                JOIN projects p ON p.id=t.project_id
                WHERE t.id=$1
                FOR UPDATE
                """,
                task_id,
            )
            if not row:
                return web.json_response({"ok": False, "error": "task_not_found"}, status=404)
            if str(row["kind"] or "task").lower() == "super":
                return web.json_response({"ok": False, "error": "super_task_not_supported"}, status=409)
            current_status = str(row["status"] or "todo").lower()
            if current_status in {"done", "postponed"}:
                return web.json_response({"ok": False, "error": "task_not_active"}, status=409)

            if previous_focus_id == task_id:
                previous_status = _focus_previous_status(focus_state, task_id)
            else:
                previous_status = current_status if current_status != "in_progress" else None

            if current_status != "in_progress":
                await conn.execute(
                    "UPDATE tasks SET status='in_progress', updated_at=NOW() WHERE id=$1",
                    task_id,
                )
                await db_add_event(
                    conn,
                    "task_in_progress",
                    int(row["project_id"]),
                    task_id,
                    f"iOS focus: [{row['project_code']}] #{task_id} {row['title']}",
                )

            await set_conversation_state(
                conn,
                int(ctx.deps.admin_id or 0),
                "attention_focus",
                step="active",
                payload={
                    "task_id": task_id,
                    "started_at": started_at,
                    "previous_status": previous_status,
                },
                ttl_sec=None,
            )

    return web.json_response(
        {
            "ok": True,
            "task_id": task_id,
            "status": "in_progress",
            "focused_since": started_at,
        }
    )

async def handle_task_steps(request: web.Request, ctx) -> web.StreamResponse:
    if not _authorized(request):
        return _auth_error()

    pool: asyncpg.Pool | None = ctx.deps.db_pool
    llm = getattr(ctx.deps, "llm", None)
    if not pool:
        return web.json_response({"ok": False, "error": "db_unavailable"}, status=503)
    if llm is None or not getattr(llm, "enabled", False):
        return web.json_response({"ok": False, "error": "llm_unavailable"}, status=503)

    try:
        task_id = int(request.match_info["task_id"])
    except Exception:
        return web.json_response({"ok": False, "error": "invalid_task_id"}, status=400)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT t.id, t.title, t.status, t.kind, p.code AS project_code
            FROM tasks t
            JOIN projects p ON p.id=t.project_id
            WHERE t.id=$1
            """,
            task_id,
        )

    if not row:
        return web.json_response({"ok": False, "error": "task_not_found"}, status=404)
    if str(row["kind"] or "task").lower() == "super":
        return web.json_response({"ok": False, "error": "super_task_not_supported"}, status=409)
    if str(row["status"] or "").lower() in {"done", "postponed"}:
        return web.json_response({"ok": False, "error": "task_not_active"}, status=409)

    title = str(row["title"] or "").strip()
    project = str(row["project_code"] or "").strip()
    system_prompt = (
        "You help a person start a task when initiation feels difficult. "
        "Return JSON only with the shape {\"steps\":[\"...\"]}. "
        "Give exactly one concrete physical next action: the smallest useful action. "
        "The step must be short, specific, and directly doable. "
        "Do not add motivation, explanation, priorities, deadlines, or new tasks. "
        "Use the same language as the task title."
    )
    user_prompt = f"Task: {title}"
    if project and project.upper() != "INBOX":
        user_prompt += f"\nProject: {project}"

    response_schema = {
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 1,
            }
        },
        "required": ["steps"],
        "additionalProperties": False,
    }

    try:
        payload = await llm.generate_json(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_schema=response_schema,
        )
    except Exception:
        return web.json_response({"ok": False, "error": "llm_unavailable"}, status=503)

    steps = _clean_task_steps(payload)[:1]
    if not steps:
        return web.json_response({"ok": False, "error": "invalid_llm_response"}, status=502)

    return web.json_response(
        {
            "ok": True,
            "task_id": task_id,
            "steps": steps,
        }
    )


async def handle_task_unfocus(request: web.Request, ctx) -> web.StreamResponse:
    if not _authorized(request, allow_widget=True):
        return _auth_error(allow_widget=True)

    pool: asyncpg.Pool | None = ctx.deps.db_pool
    if not pool:
        return web.json_response({"ok": False, "error": "db_unavailable"}, status=503)

    try:
        task_id = int(request.match_info["task_id"])
    except Exception:
        return web.json_response({"ok": False, "error": "invalid_task_id"}, status=400)

    async with pool.acquire() as conn:
        async with conn.transaction():
            focus_state = await get_conversation_state(
                conn,
                int(ctx.deps.admin_id or 0),
                "attention_focus",
            )
            try:
                focused_id = int((focus_state or {}).get("payload", {}).get("task_id"))
            except (TypeError, ValueError):
                focused_id = None

            if focused_id == task_id:
                previous_status = _focus_previous_status(focus_state, task_id)
                if previous_status:
                    await conn.execute(
                        """
                        UPDATE tasks
                        SET status=$2, updated_at=NOW()
                        WHERE id=$1 AND status='in_progress'
                        """,
                        task_id,
                        previous_status,
                    )
                await clear_conversation_state(
                    conn,
                    int(ctx.deps.admin_id or 0),
                    "attention_focus",
                )

    return web.json_response({"ok": True, "task_id": task_id, "status": "unfocused"})

async def handle_task_done(request: web.Request, ctx) -> web.StreamResponse:
    if not _authorized(request, allow_widget=True):
        return _auth_error(allow_widget=True)

    pool: asyncpg.Pool | None = ctx.deps.db_pool
    if not pool:
        return web.json_response({"ok": False, "error": "db_unavailable"}, status=503)

    try:
        task_id = int(request.match_info["task_id"])
    except Exception:
        return web.json_response({"ok": False, "error": "invalid_task_id"}, status=400)

    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT t.id, t.title, t.status, t.kind, t.project_id, p.code AS project_code
                FROM tasks t
                JOIN projects p ON p.id=t.project_id
                WHERE t.id=$1
                FOR UPDATE
                """,
                task_id,
            )
            if not row:
                return web.json_response({"ok": False, "error": "task_not_found"}, status=404)
            if str(row["kind"] or "task").lower() == "super":
                return web.json_response({"ok": False, "error": "super_task_not_supported"}, status=409)
            if str(row["status"] or "todo").lower() != "done":
                await conn.execute(
                    "UPDATE tasks SET status='done', updated_at=NOW() WHERE id=$1",
                    task_id,
                )
                await db_add_event(
                    conn,
                    "task_done",
                    int(row["project_id"]),
                    task_id,
                    f"iOS done: [{row['project_code']}] #{task_id} {row['title']}",
                )

            focus_state = await get_conversation_state(
                conn,
                int(ctx.deps.admin_id or 0),
                "attention_focus",
            )
            try:
                focused_id = int((focus_state or {}).get("payload", {}).get("task_id"))
            except (TypeError, ValueError):
                focused_id = None
            if focused_id == task_id:
                await clear_conversation_state(
                    conn,
                    int(ctx.deps.admin_id or 0),
                    "attention_focus",
                )

    return web.json_response({"ok": True, "task_id": task_id, "status": "done"})
