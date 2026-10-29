"""Undo the latest recorded capture: task, reminder, idea or calendar event.

Only recent entries are undone, so «Отменить» can never remove something
created long ago.
"""
from __future__ import annotations

import asyncpg

from bot.db import db_add_event
from bot.db.runtime_state import forget_recent_action, mark_action_undone

UNDO_WINDOW_MINUTES = 15


async def _latest_recent_entry(conn: asyncpg.Connection, chat_id: int) -> dict | None:
    row = await conn.fetchrow(
        """
        SELECT id, undo_payload_json
        FROM action_journal
        WHERE chat_id=$1
          AND undone_at IS NULL
          AND undo_payload_json <> '{}'::jsonb
          AND created_at >= NOW() - make_interval(mins => $2)
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        int(chat_id),
        UNDO_WINDOW_MINUTES,
    )
    if not row:
        return None
    undo = row["undo_payload_json"]
    if isinstance(undo, str):
        import json

        undo = json.loads(undo or "{}")
    return {"id": int(row["id"]), "undo": dict(undo or {})}


async def undo_latest(conn: asyncpg.Connection, *, chat_id: int, icloud=None) -> str:
    """Caller owns the transaction. Returns a short human message."""
    entry = await _latest_recent_entry(conn, chat_id)
    if not entry:
        return "Нечего отменять."
    undo = entry["undo"]
    action = str(undo.get("action") or "").strip().lower()
    message = "Нечего отменять."

    if action in {"task", "personal_task"}:
        task_id = int(undo.get("task_id") or 0)
        row = await conn.fetchrow("SELECT project_id, title FROM tasks WHERE id=$1", task_id)
        if row:
            await conn.execute("DELETE FROM tasks WHERE id=$1", task_id)
            await db_add_event(conn, "task_undo", int(row["project_id"]), None, f"↩️ Отмена задачи #{task_id} {row['title']}")
            message = f"↩️ Отменено: {row['title']}"
        else:
            message = "Задача уже удалена."
    elif action == "reminder":
        reminder_id = int(undo.get("reminder_id") or 0)
        row = await conn.fetchrow("SELECT text, status FROM reminders WHERE id=$1", reminder_id)
        if row and str(row["status"] or "") not in {"sent", "cancelled"}:
            await conn.execute(
                """
                UPDATE reminders
                SET status='cancelled', cancelled_at_utc=NOW(), claim_token=NULL, claimed_at_utc=NULL
                WHERE id=$1
                """,
                reminder_id,
            )
            await db_add_event(conn, "reminder_undo", None, None, f"↩️ Отмена напоминания: {row['text']}")
            message = f"↩️ Напоминание отменено: {row['text']}"
        else:
            message = "Напоминание уже отправлено или отменено."
    elif action == "idea":
        idea_id = int(undo.get("idea_id") or 0)
        row = await conn.fetchrow("SELECT text FROM ideas WHERE id=$1 AND chat_id=$2", idea_id, int(chat_id))
        if row:
            await conn.execute("DELETE FROM ideas WHERE id=$1 AND chat_id=$2", idea_id, int(chat_id))
            await db_add_event(conn, "idea_undo", None, None, f"↩️ Отмена идеи: {row['text']}")
            message = f"↩️ Идея удалена: {row['text']}"
        else:
            message = "Идея уже удалена."
    elif action == "event":
        summary = str(undo.get("summary") or "событие")
        ics_url = str(undo.get("ics_url") or "")
        if ics_url:
            if icloud is None or not await icloud.delete_event(ics_url):
                return "Не удалось удалить событие из календаря."
            await conn.execute("DELETE FROM icloud_events WHERE ics_url=$1", ics_url)
        await db_add_event(conn, "ical_event_undo", None, None, f"↩️ Отмена события: {summary}")
        message = f"↩️ Событие отменено: {summary}"

    await mark_action_undone(conn, entry["id"])
    fingerprint = str(undo.get("fingerprint") or "").strip()
    if fingerprint:
        await forget_recent_action(conn, chat_id=int(chat_id), fingerprint=fingerprint)
    return message
