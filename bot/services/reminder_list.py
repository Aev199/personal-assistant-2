"""Upcoming reminders for every client: list, delete, turn into a task."""
from __future__ import annotations

from bot.db import db_add_event, ensure_inbox_project_id

_ACTIVE = """r.status IN ('pending', 'retry', 'claimed')
             AND r.cancelled_at_utc IS NULL
             AND COALESCE(r.is_sent, FALSE) = FALSE
             AND (r.chat_id = $1 OR r.chat_id IS NULL)"""


class ReminderNotActive(Exception):
    pass


async def list_upcoming(conn, *, chat_id: int, limit: int = 100) -> list[dict]:
    rows = await conn.fetch(
        f"""
        SELECT r.id, r.text, r.remind_at, r.repeat
        FROM reminders r
        WHERE {_ACTIVE}
        ORDER BY r.remind_at ASC, r.id ASC
        LIMIT $2
        """,
        int(chat_id),
        int(limit),
    )
    return [dict(row) for row in rows]


async def _lock_active(conn, *, chat_id: int, reminder_id: int):
    row = await conn.fetchrow(
        f"SELECT r.id, r.text, r.repeat FROM reminders r WHERE r.id=$2 AND {_ACTIVE} FOR UPDATE",
        int(chat_id),
        int(reminder_id),
    )
    if not row:
        raise ReminderNotActive()
    return row


async def _cancel(conn, reminder_id: int) -> None:
    await conn.execute(
        """
        UPDATE reminders
        SET status='cancelled', cancelled_at_utc=NOW(), claim_token=NULL, claimed_at_utc=NULL
        WHERE id=$1
        """,
        int(reminder_id),
    )


async def cancel_reminder(conn, *, chat_id: int, reminder_id: int) -> str:
    """Caller owns the transaction. Returns the reminder text."""
    row = await _lock_active(conn, chat_id=chat_id, reminder_id=reminder_id)
    await _cancel(conn, reminder_id)
    await db_add_event(conn, "reminder_cancelled", None, None, f"🗑 Напоминание удалено: {row['text']}")
    return str(row["text"])


async def reminder_to_task(conn, *, chat_id: int, reminder_id: int) -> dict:
    """An Inbox task with the reminder time as its deadline; the reminder is removed.

    For a repeating reminder the whole series is removed: a task is one-off.
    """
    row = await _lock_active(conn, chat_id=chat_id, reminder_id=reminder_id)
    project_id = int(await ensure_inbox_project_id(conn))
    # remind_at is copied in SQL, so TIMESTAMP and TIMESTAMPTZ schemas both work.
    task_id = int(
        await conn.fetchval(
            """
            INSERT INTO tasks (project_id, title, status, kind, deadline)
            SELECT $1, r.text, 'todo', 'task', r.remind_at FROM reminders r WHERE r.id=$2
            RETURNING id
            """,
            project_id,
            int(reminder_id),
        )
    )
    await _cancel(conn, reminder_id)
    await db_add_event(conn, "reminder_to_task", project_id, task_id, f"📝 Напоминание стало задачей: {row['text']}")
    return {"task_id": task_id, "title": str(row["text"])}
