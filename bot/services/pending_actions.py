"""Draft/confirm execution for LLM-originated actions."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.db import db_add_event
from bot.db.runtime_state import (
    create_pending_action,
    mark_pending_action_status,
    record_action_journal,
    remember_recent_action,
)
from bot.deps import AppDeps
from bot.services.gtasks_service import due_from_local_date, get_or_create_list_id
from bot.services.vault_sync import background_project_sync
from bot.tz import fmt_local, to_db_utc
from bot.utils import h
from bot.services.background import fire_and_forget


def _parse_iso_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except Exception:
        return None


def _can_write(conn: Any) -> bool:
    return hasattr(conn, "execute") and hasattr(conn, "fetchval")


def _preview_text(kind: str, payload: dict[str, Any], *, tz_name: str) -> str:
    header = "⏳ ЧЕРНОВИК\n"
    if kind == "task":
        bits = [f"Создать задачу: {payload.get('title') or ''}"]
        if payload.get("project_code"):
            bits.append(f"Проект: {payload['project_code']}")
        if payload.get("assignee_name"):
            bits.append(f"Исполнитель: {payload['assignee_name']}")
        dt = _parse_iso_dt(payload.get("deadline_local"))
        if dt:
            bits.append(f"Срок: {dt.strftime('%d.%m %H:%M')}")
        return header + "\n".join(bits)
    if kind == "personal_task":
        bits = [f"Добавить личную задачу: {payload.get('title') or ''}"]
        dt = _parse_iso_dt(payload.get("deadline_local"))
        if dt:
            bits.append(f"Срок: {dt.strftime('%d.%m %H:%M')}")
        return header + "\n".join(bits)
    if kind == "event":
        bits = [f"Создать событие: {payload.get('title') or ''}"]
        bits.append(f"Календарь: {'рабочий' if payload.get('calendar_kind') == 'work' else 'личный'}")
        dt = _parse_iso_dt(payload.get("start_local"))
        if dt:
            bits.append(f"Старт: {dt.strftime('%d.%m %H:%M')}")
        if payload.get("duration_min"):
            bits.append(f"Длительность: {payload['duration_min']} мин")
        if payload.get("project_code"):
            bits.append(f"Проект: {payload['project_code']}")
        return header + "\n".join(bits)
    if kind == "idea":
        return header + f"Добавить идею:\n{payload.get('idea_text') or ''}"
    if kind == "reminder":
        dt = _parse_iso_dt(payload.get("remind_at_local"))
        when = dt.strftime("%d.%m %H:%M") if dt else "?"
        return header + f"Создать напоминание:\n{payload.get('reminder_text') or ''}\nКогда: {when}"
    return header + "Подтвердите действие."


async def create_pending_preview(
    message: Message,
    *,
    db_pool: asyncpg.Pool,
    deps: AppDeps,
    kind: str,
    payload: dict[str, Any],
    fingerprint: str,
    summary: str,
    source: str,
) -> int:
    ttl_sec = max(60, int(os.getenv("PENDING_ACTION_TTL_SEC", "900")))
    async with db_pool.acquire() as conn:
        pending_action_id = await create_pending_action(
            conn,
            chat_id=int(message.chat.id),
            kind=kind,
            payload={**payload, "source": source},
            source_message_id=int(getattr(message, "message_id", 0) or 0),
            fingerprint=fingerprint,
            ttl_sec=ttl_sec,
        )
        await remember_recent_action(
            conn,
            chat_id=int(message.chat.id),
            fingerprint=fingerprint,
            action=kind,
            summary=summary,
            pending_action_id=int(pending_action_id),
            ttl_sec=max(45, ttl_sec),
        )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"llm:confirm:{pending_action_id}"),
                InlineKeyboardButton(text="✖ Отмена", callback_data=f"llm:cancel:{pending_action_id}"),
            ]
        ]
    )
    if hasattr(message, "answer"):
        await message.answer(_preview_text(kind, payload, tz_name=deps.tz_name), reply_markup=kb)
    return int(pending_action_id)


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
        fire_and_forget(
            background_project_sync(
                int(payload["project_id"]),
                db_pool,
                deps.vault,
                error_logger=(deps.db_log_error or (lambda _w, _e, _c=None: None)),
            ),
            label="vault_sync",
        )
        return f"✅ Задача создана: {payload.get('title') or ''}"

    if kind == "personal_task":
        gtasks = getattr(deps, "gtasks", None)
        if gtasks is None or not gtasks.enabled():
            raise RuntimeError("Google Tasks не настроен")
        due_local = _parse_iso_dt(payload.get("deadline_local"))
        due_utc = due_from_local_date(due_local, timezone.utc) if due_local else None
        list_name = os.getenv("GTASKS_PERSONAL_LIST", "Личное")
        list_id = await get_or_create_list_id(db_pool, gtasks, list_name)
        created = await gtasks.create_task(list_id, str(payload.get("title") or ""), due=due_utc)
        g_task_id = str((created or {}).get("id") or "")
        async with db_pool.acquire() as conn:
            if _can_write(conn):
                await db_add_event(conn, "personal_task_created", None, None, f"LLM/{source}: {payload.get('title') or ''}")
                await record_action_journal(
                    conn,
                    chat_id=chat_id,
                    source="llm",
                    action_type="personal_task",
                    summary=str(payload.get("title") or ""),
                    payload={"pending_action_id": pending_action_id},
                    undo_payload={
                        "action": "personal_task",
                        "list_id": list_id,
                        "g_task_id": g_task_id,
                        "title": str(payload.get("title") or ""),
                        "fingerprint": fingerprint,
                    },
                    action_key=f"pending-confirm:{pending_action_id}",
                )
                await mark_pending_action_status(conn, pending_action_id=pending_action_id, status="executed")
        return f"✅ Личная задача создана: {payload.get('title') or ''}"

    if kind == "idea":
        gtasks = getattr(deps, "gtasks", None)
        if gtasks is None or not gtasks.enabled():
            raise RuntimeError("Google Tasks не настроен")
        list_name = os.getenv("GTASKS_IDEAS_LIST", "Идеи")
        list_id = await get_or_create_list_id(db_pool, gtasks, list_name)
        created = await gtasks.create_task(list_id, str(payload.get("idea_text") or ""))
        g_task_id = str((created or {}).get("id") or "")
        async with db_pool.acquire() as conn:
            if _can_write(conn):
                await db_add_event(conn, "idea_captured", None, None, f"LLM/{source}: {payload.get('idea_text') or ''}")
                await record_action_journal(
                    conn,
                    chat_id=chat_id,
                    source="llm",
                    action_type="idea",
                    summary=str(payload.get("idea_text") or ""),
                    payload={"pending_action_id": pending_action_id},
                    undo_payload={
                        "action": "idea",
                        "list_id": list_id,
                        "g_task_id": g_task_id,
                        "title": str(payload.get("idea_text") or ""),
                        "fingerprint": fingerprint,
                    },
                    action_key=f"pending-confirm:{pending_action_id}",
                )
                await mark_pending_action_status(conn, pending_action_id=pending_action_id, status="executed")
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
        if payload.get("calendar_kind") == "work" and payload.get("project_id"):
            fire_and_forget(
                background_project_sync(
                    int(payload["project_id"]),
                    db_pool,
                    deps.vault,
                    error_logger=(deps.db_log_error or (lambda _w, _e, _c=None: None)),
                ),
                label="vault_sync",
            )
        if synced:
            return "✅ Событие создано и синхронизировано с iCloud"
        return "⚠️ Событие сохранено локально. Синхронизация с iCloud запланирована."

    raise RuntimeError(f"Unsupported pending action kind: {kind}")
