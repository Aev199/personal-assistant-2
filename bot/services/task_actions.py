"""Small task actions shared by the Telegram screens: complete and set a due date."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from bot.db import db_add_event
from bot.tz import resolve_tz_name

DUE_HOUR = 18
DUE_CHOICES = ("today", "tomorrow", "week", "none")


class TaskActionError(Exception):
    pass


async def _lock_active(conn, task_id: int):
    row = await conn.fetchrow(
        "SELECT id, title, status, kind, project_id FROM tasks WHERE id=$1 FOR UPDATE",
        int(task_id),
    )
    if not row or row["kind"] == "super" or row["status"] in ("done", "postponed"):
        raise TaskActionError("task_not_active")
    return row


async def complete_task(conn, *, task_id: int) -> str:
    """Caller owns the transaction. Returns the task title."""
    row = await _lock_active(conn, task_id)
    await conn.execute("UPDATE tasks SET status='done' WHERE id=$1", int(task_id))
    await db_add_event(conn, "task_done", int(row["project_id"]), int(task_id), f"✅ {row['title']}")
    return str(row["title"])


def due_for(choice: str, *, tz_name: str | None, now: datetime | None = None) -> datetime | None:
    """UTC-naive deadline for a quick due choice (project convention)."""
    if choice not in DUE_CHOICES:
        raise TaskActionError("invalid_due")
    if choice == "none":
        return None
    tz = ZoneInfo(resolve_tz_name(tz_name))
    now_local = (now or datetime.now(timezone.utc)).astimezone(tz)
    day: date = now_local.date() + {"today": timedelta(0), "tomorrow": timedelta(days=1), "week": timedelta(days=7)}[choice]
    local = datetime.combine(day, time(DUE_HOUR), tz)
    if choice == "today" and local <= now_local:
        local = datetime.combine(day, time(23, 0), tz)
    return local.astimezone(timezone.utc).replace(tzinfo=None)


async def set_due(conn, *, task_id: int, choice: str, tz_name: str | None, store_tz: bool = False) -> None:
    """Some deployments keep TIMESTAMPTZ deadlines; those need an aware value."""
    row = await _lock_active(conn, task_id)
    deadline = due_for(choice, tz_name=tz_name)
    if deadline is not None and store_tz:
        deadline = deadline.replace(tzinfo=timezone.utc)
    await conn.execute("UPDATE tasks SET deadline=$2 WHERE id=$1", int(task_id), deadline)
    await db_add_event(conn, "task_deadline", int(row["project_id"]), int(task_id), f"срок: {choice}")
