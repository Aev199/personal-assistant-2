"""Draft/confirm execution for LLM-originated actions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg

from bot.db import db_add_event, ensure_inbox_project_id, ensure_personal_project_id
from bot.db.runtime_state import (
    mark_pending_action_status,
    record_action_journal,
)
from bot.deps import AppDeps
from bot.tz import to_db_utc


def _parse_iso_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except Exception:
        return None


async def execute_pending_action(
    pending_action: dict[str, Any],
    *,
    db_pool: asyncpg.Pool,
    deps: AppDeps,
    chat_id: int,
) -> str:
    kind = str(pending_action["kind"])
    payload = dict(pending_action.get("payload") or {})
    source = str(payload.get("source") or "llm")
    fingerprint = str(pending_action.get("fingerprint") or payload.get("fingerprint") or "")
    pending_action_id = int(pending_action["id"])

    if kind == "task":
        deadline_local = _parse_iso_dt(payload.get("deadline_local"))
        async with db_pool.acquire() as conn:
            task_id = await conn.fetchval(
                """
                INSERT INTO tasks (project_id, title, assignee_id, deadline)
                VALUES ($1, $2, $3, $4)
                RETURNING id
                """,
                int(payload["project_id"]),
                str(payload["title"]),
                payload.get("assignee_id"),
                to_db_utc(
                    deadline_local,
                    tz_name=deps.tz_name,
                    store_tz=bool(getattr(deps, "db_tasks_deadline_timestamptz", False)),
                )
                if deadline_local
                else None,
            )
            await db_add_event(
                conn,
                "task_created",
                int(payload["project_id"]),
                int(task_id),
                f"LLM/{source}: [{payload.get('project_code') or ''}] #{int(task_id)} {payload.get('title') or ''}",
            )
            await record_action_journal(
                conn,
                chat_id=chat_id,
                source="llm",
                action_type="task",
                summary=str(payload.get("title") or ""),
                payload={"pending_action_id": pending_action_id},
                undo_payload={
                    "action": "task",
                    "task_id": int(task_id),
                    "project_id": int(payload["project_id"]),
                    "title": str(payload.get("title") or ""),
                    "fingerprint": fingerprint,
                },
                action_key=f"pending-confirm:{pending_action_id}",
            )
            await mark_pending_action_status(conn, pending_action_id=pending_action_id, status="executed")
        return f"✅ Задача создана: {payload.get('title') or ''}"

    if kind == "personal_task":
        due_local = _parse_iso_dt(payload.get("deadline_local"))
        async with db_pool.acquire() as conn:
            project_id = await ensure_personal_project_id(conn)
            task_id = await conn.fetchval(
                """
                INSERT INTO tasks (project_id, title, status, kind, deadline)
                VALUES ($1, $2, 'todo', 'personal', $3)
                RETURNING id
                """,
                int(project_id),
                str(payload.get("title") or ""),
                to_db_utc(
                    due_local,
                    tz_name=deps.tz_name,
                    store_tz=bool(getattr(deps, "db_tasks_deadline_timestamptz", False)),
                )
                if due_local
                else None,
            )
            await db_add_event(
                conn,
                "personal_task_created",
                int(project_id),
                int(task_id),
                f"LLM/{source}: {payload.get('title') or ''}",
            )
            await record_action_journal(
                conn,
                chat_id=chat_id,
                source="llm",
                action_type="personal_task",
                summary=str(payload.get("title") or ""),
                payload={"pending_action_id": pending_action_id},
                undo_payload={
                    "action": "personal_task",
                    "task_id": int(task_id),
                    "project_id": int(project_id),
                    "title": str(payload.get("title") or ""),
                    "fingerprint": fingerprint,
                },
                action_key=f"pending-confirm:{pending_action_id}",
            )
            await mark_pending_action_status(
                conn,
                pending_action_id=pending_action_id,
                status="executed",
            )
        return f"✅ Личная задача создана: {payload.get('title') or ''}"

    if kind == "idea":
        text_value = str(payload.get("idea_text") or "").strip()
        if not text_value:
            raise RuntimeError("Пустая идея")
        async with db_pool.acquire() as conn:
            idea_id = await conn.fetchval(
                """
                INSERT INTO ideas (chat_id, text, source, status)
                VALUES ($1, $2, $3, 'active')
                RETURNING id
                """,
                int(chat_id),
                text_value,
                source,
            )
            await db_add_event(
                conn,
                "idea_captured",
                None,
                None,
                f"LLM/{source}: {text_value}",
            )
            await record_action_journal(
                conn,
                chat_id=chat_id,
                source="llm",
                action_type="idea",
                summary=text_value,
                payload={"pending_action_id": pending_action_id},
                undo_payload={
                    "action": "idea",
                    "idea_id": int(idea_id),
                    "title": text_value,
                    "fingerprint": fingerprint,
                },
                action_key=f"pending-confirm:{pending_action_id}",
            )
            await mark_pending_action_status(
                conn,
                pending_action_id=pending_action_id,
                status="executed",
            )
        return "✅ Идея сохранена"

    if kind == "reminder":
        remind_local = _parse_iso_dt(payload.get("remind_at_local"))
        if remind_local is None:
            raise RuntimeError("Не удалось разобрать время напоминания")
        async with db_pool.acquire() as conn:
            reminder_id = await conn.fetchval(
                """
                INSERT INTO reminders (
                    chat_id,
                    text,
                    remind_at,
                    repeat,
                    status,
                    next_attempt_at_utc,
                    is_sent
                )
                VALUES ($1, $2, $3, 'none', 'pending', $4, FALSE)
                RETURNING id
                """,
                int(chat_id),
                str(payload.get("reminder_text") or ""),
                to_db_utc(
                    remind_local,
                    tz_name=deps.tz_name,
                    store_tz=bool(getattr(deps, "db_reminders_remind_at_timestamptz", False)),
                ),
                remind_local.astimezone(timezone.utc),
            )
            await db_add_event(conn, "reminder_created", None, None, f"LLM/{source}: {payload.get('reminder_text') or ''}")
            await record_action_journal(
                conn,
                chat_id=chat_id,
                source="llm",
                action_type="reminder",
                summary=str(payload.get("reminder_text") or ""),
                payload={"pending_action_id": pending_action_id},
                undo_payload={
                    "action": "reminder",
                    "reminder_id": int(reminder_id),
                    "text": str(payload.get("reminder_text") or ""),
                    "fingerprint": fingerprint,
                },
                action_key=f"pending-confirm:{pending_action_id}",
            )
            await mark_pending_action_status(conn, pending_action_id=pending_action_id, status="executed")
        return "✅ Напоминание создано"

    if kind == "event":
        icloud = getattr(deps, "icloud", None)
        if icloud is None:
            raise RuntimeError("iCloud не настроен")
        start_local = _parse_iso_dt(payload.get("start_local"))
        if start_local is None:
            raise RuntimeError("Не удалось разобрать время события")
        duration_min = int(payload.get("duration_min") or 0)
        dtstart_utc = start_local.astimezone(timezone.utc)
        dtend_utc = dtstart_utc + timedelta(minutes=duration_min)
        async with db_pool.acquire() as conn:
            if payload.get("calendar_kind") == "work" and not payload.get("project_id"):
                inbox_project_id = await ensure_inbox_project_id(conn)
                payload["project_id"] = int(inbox_project_id)
                payload["project_code"] = "INBOX"
                payload["project_name"] = "INBOX"

            # Guard against duplicate event creation
            existing_event = await conn.fetchval(
                """
                SELECT id FROM icloud_events
                WHERE calendar_url=$1 AND summary=$2 AND dtstart_utc=$3
                LIMIT 1
                """,
                str(payload["calendar_url"]),
                str(payload["summary"]),
                dtstart_utc,
            )
            if existing_event:
                event_id = int(existing_event)
            else:
                event_id = int(
                    await conn.fetchval(
                        """
                        INSERT INTO icloud_events (
                            calendar_url, summary, dtstart_utc, dtend_utc,
                            description, location, sync_status, pending_action_id
                        )
                        VALUES ($1, $2, $3, $4, '', '', 'pending', $5)
                        RETURNING id
                        """,
                        str(payload["calendar_url"]),
                        str(payload["summary"]),
                        dtstart_utc,
                        dtend_utc,
                        int(pending_action_id),
                    )
                )
            external_uid = f"assistant-event-{event_id}@local"
            ics_url, success = await icloud.create_event(
                calendar_url=str(payload["calendar_url"]),
                summary=str(payload["summary"]),
                dtstart_utc=dtstart_utc,
                dtend_utc=dtend_utc,
                uid=external_uid,
            )
            synced = False
            if success:
                synced = True
                await conn.execute(
                    """
                    UPDATE icloud_events
                    SET sync_status='synced', ics_url=$2, external_uid=$3
                    WHERE id=$1
                    """,
                    event_id,
                    ics_url,
                    external_uid,
                )
            else:
                await conn.execute(
                    """
                    UPDATE icloud_events
                    SET sync_status='pending', last_error='Initial sync failed', external_uid=$2
                    WHERE id=$1
                    """,
                    event_id,
                    external_uid,
                )
            await db_add_event(
                conn,
                "ical_event_created",
                int(payload.get("project_id") or 0) or None,
                None,
                f"LLM/{source}: {payload.get('summary') or ''}",
            )
            await record_action_journal(
                conn,
                chat_id=chat_id,
                source="llm",
                action_type="event",
                summary=str(payload.get("summary") or ""),
                payload={"pending_action_id": pending_action_id},
                undo_payload={
                    "action": "event",
                    "project_id": int(payload.get("project_id") or 0) or None,
                    "calendar_url": str(payload["calendar_url"]),
                    "summary": str(payload["summary"]),
                    "dtstart_utc": dtstart_utc.isoformat(),
                    "dtend_utc": dtend_utc.isoformat(),
                    "ics_url": ics_url,
                    "fingerprint": fingerprint,
                },
                action_key=f"pending-confirm:{pending_action_id}",
            )
            await mark_pending_action_status(conn, pending_action_id=pending_action_id, status="executed")
        if synced:
            return "✅ Событие создано и синхронизировано с iCloud"
        return "⚠️ Событие сохранено локально. Синхронизация с iCloud запланирована."

    raise RuntimeError(f"Unsupported pending action kind: {kind}")
