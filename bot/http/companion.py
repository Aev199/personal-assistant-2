"""HTTP API shared by native Assistant clients.

The backend owns task selection and mutations. iOS, widgets, shortcuts and
Telegram are interfaces over the same data; legacy /companion routes remain
as compatibility aliases while native clients move to canonical /api/v1 routes.
"""

from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import asyncpg
from aiohttp import web

from bot.db import db_add_event, ensure_inbox_project_id
from bot.tz import resolve_tz_name


MAX_CAPTURE_LEN = 2000
ATTENTION_TASK_LIMIT = 5
ATTENTION_URGENT_LIMIT = 3


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


def _utc_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _attention_sort_key(row: dict, now_utc: datetime, end_utc_aware: datetime) -> tuple:
    deadline = _utc_aware(row.get("deadline"))
    created_at = _utc_aware(row.get("created_at"))
    status = str(row.get("status") or "").lower()
    task_id = int(row.get("id") or 0)

    if status == "in_progress":
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
) -> list[dict]:
    """Keep the daily surface useful without letting overdue work consume it."""
    ordered = sorted((dict(row) for row in rows or []), key=lambda row: _attention_sort_key(row, now_utc, end_utc_aware))
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

    async def _capture(request: web.Request) -> web.StreamResponse:
        return await handle_capture(request, ctx)

    async def _done(request: web.Request) -> web.StreamResponse:
        return await handle_task_done(request, ctx)

    # Canonical client API.
    app.router.add_get("/api/v1/today", _today)
    app.router.add_get("/api/v1/tasks", _tasks)
    app.router.add_post("/api/v1/capture", _capture)
    app.router.add_post("/api/v1/tasks/{task_id}/done", _done)

    # Compatibility for already installed companion builds.
    app.router.add_get("/api/v1/companion/today", _today)
    app.router.add_post("/api/v1/companion/capture", _capture)
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

    async with pool.acquire() as conn:
        task_rows = await conn.fetch(
            """
            SELECT t.id, t.title, t.deadline, t.status, t.created_at,
                   p.code AS project_code, COALESCE(tm.name, '') AS assignee
            FROM tasks t
            JOIN projects p ON p.id=t.project_id
            LEFT JOIN team tm ON tm.id=t.assignee_id
            WHERE t.status NOT IN ('done', 'postponed')
              AND t.kind != 'super'
              AND p.status='active'
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
              AND remind_at >= $2
              AND remind_at < $3
            ORDER BY remind_at ASC, id ASC
            LIMIT 50
            """,
            int(ctx.deps.admin_id or 0),
            start_utc,
            end_utc,
        )

    selected_task_rows = _select_attention_tasks(
        task_rows,
        now_utc=now_utc,
        end_utc_aware=end_local.astimezone(timezone.utc),
    )

    tasks = []
    for row in selected_task_rows:
        deadline_utc = _utc_aware(row["deadline"])
        deadline_local = deadline_utc.astimezone(tz) if deadline_utc else None
        tasks.append(
            {
                "id": int(row["id"]),
                "title": str(row["title"] or ""),
                "project": str(row["project_code"] or ""),
                "assignee": str(row["assignee"] or ""),
                "status": str(row["status"] or "todo"),
                "deadline": deadline_local.isoformat() if deadline_local else None,
                "overdue": bool(deadline_utc and deadline_utc < now_utc),
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

    return web.json_response(
        {
            "ok": True,
            "date": start_local.date().isoformat(),
            "timezone": tz_name,
            "tasks": tasks,
            "reminders": reminders,
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

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT t.id, t.title, t.deadline, t.status, t.created_at,
                   p.code AS project_code, COALESCE(tm.name, '') AS assignee
            FROM tasks t
            JOIN projects p ON p.id=t.project_id
            LEFT JOIN team tm ON tm.id=t.assignee_id
            WHERE t.status NOT IN ('done', 'postponed')
              AND t.kind != 'super'
              AND p.status='active'
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
                "project": str(row["project_code"] or ""),
                "assignee": str(row["assignee"] or ""),
                "status": str(row["status"] or "todo"),
                "deadline": deadline_local.isoformat() if deadline_local else None,
                "overdue": bool(deadline_utc and deadline_utc < now_utc),
            }
        )

    return web.json_response(
        {
            "ok": True,
            "timezone": tz_name,
            "tasks": tasks,
        }
    )


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

    return web.json_response({"ok": True, "task_id": task_id, "status": "done"})
