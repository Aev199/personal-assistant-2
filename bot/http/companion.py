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
from bot.tz import resolve_tz_name, to_db_utc
from bot.services.native_intake import process_native_capture, confirm_native_action, cancel_native_action
from bot.db.runtime_state import get_conversation_state, set_conversation_state, clear_conversation_state


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

    async def _task_update(request: web.Request) -> web.StreamResponse:
        return await handle_task_update(request, ctx)

    async def _capture(request: web.Request) -> web.StreamResponse:
        return await handle_capture(request, ctx)

    async def _intake(request: web.Request) -> web.StreamResponse:
        return await handle_intake(request, ctx)

    async def _intake_pending(request: web.Request) -> web.StreamResponse:
        return await handle_intake_pending(request, ctx)

    async def _intake_confirm(request: web.Request) -> web.StreamResponse:
        return await handle_intake_confirm(request, ctx)

    async def _intake_cancel(request: web.Request) -> web.StreamResponse:
        return await handle_intake_cancel(request, ctx)

    async def _focus(request: web.Request) -> web.StreamResponse:
        return await handle_task_focus(request, ctx)

    async def _done(request: web.Request) -> web.StreamResponse:
        return await handle_task_done(request, ctx)

    # Canonical client API.
    app.router.add_get("/api/v1/today", _today)
    app.router.add_get("/api/v1/tasks", _tasks)
    app.router.add_get("/api/v1/projects", _projects)
    app.router.add_patch("/api/v1/tasks/{task_id}", _task_update)
    app.router.add_post("/api/v1/capture", _capture)
    app.router.add_post("/api/v1/intake", _intake)
    app.router.add_get("/api/v1/intake/pending", _intake_pending)
    app.router.add_post("/api/v1/intake/{pending_action_id}/confirm", _intake_confirm)
    app.router.add_post("/api/v1/intake/{pending_action_id}/cancel", _intake_cancel)
    app.router.add_post("/api/v1/tasks/{task_id}/focus", _focus)
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

    focus_task_id: int | None = None
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
                "project": str(row["project_code"] or ""),
                "assignee": str(row["assignee"] or ""),
                "status": str(row["status"] or "todo"),
                "deadline": deadline_local.isoformat() if deadline_local else None,
                "overdue": bool(deadline_utc and deadline_utc < now_utc),
                "focused": focus_task_id is not None and int(row["id"]) == focus_task_id,
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

    focus_task_id: int | None = None
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
                "project": str(row["project_code"] or ""),
                "assignee": str(row["assignee"] or ""),
                "status": str(row["status"] or "todo"),
                "deadline": deadline_local.isoformat() if deadline_local else None,
                "overdue": bool(deadline_utc and deadline_utc < now_utc),
                "focused": focus_task_id is not None and int(row["id"]) == focus_task_id,
            }
        )

    return web.json_response(
        {
            "ok": True,
            "timezone": tz_name,
            "tasks": tasks,
        }
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
    result = await process_native_capture(
        text=text,
        deps=ctx.deps,
        db_pool=pool,
        chat_id=int(ctx.deps.admin_id or 0),
        prepend_text=context,
        source="ios",
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
    if not _authorized(request):
        return _auth_error()

    pool: asyncpg.Pool | None = ctx.deps.db_pool
    if not pool:
        return web.json_response({"ok": False, "error": "db_unavailable"}, status=503)

    try:
        task_id = int(request.match_info["task_id"])
    except Exception:
        return web.json_response({"ok": False, "error": "invalid_task_id"}, status=400)

    async with pool.acquire() as conn:
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
        if str(row["status"] or "").lower() in {"done", "postponed"}:
            return web.json_response({"ok": False, "error": "task_not_active"}, status=409)

        if str(row["status"] or "todo").lower() != "in_progress":
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
            payload={"task_id": task_id},
            ttl_sec=None,
        )

    return web.json_response({"ok": True, "task_id": task_id, "status": "in_progress"})


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
