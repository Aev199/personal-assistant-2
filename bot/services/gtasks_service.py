"""Google Tasks helper service.

We treat Google Tasks as an *optional* integration:
 - personal (non-work) tasks are the primary use-case;
 - exporting work tasks is a fallback option.

This module keeps the DB mapping logic (tasklist title -> id) out of handlers.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import asyncpg

from bot.adapters.google_tasks_adapter import GoogleTasksAdapter


UTC = ZoneInfo("UTC")


async def get_or_create_list_id(db_pool: asyncpg.Pool, gtasks: GoogleTasksAdapter, name: str) -> str:
    """Return Google Tasks list_id for a given title (create if missing).

    Stores mapping in DB to avoid repeated list enumeration.
    """
    if not gtasks.enabled():
        raise RuntimeError("Google Tasks is not configured")

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT list_id FROM g_tasks_lists WHERE name=$1", name)
        if row and row.get("list_id"):
            return str(row["list_id"])

    # Try to find existing list by title in Google
    lists = await gtasks.list_tasklists()
    for lst in lists:
        if (lst.get("title") or "").strip() == name:
            list_id = lst.get("id")
            if list_id:
                async with db_pool.acquire() as conn:
                    await conn.execute(
                        "INSERT INTO g_tasks_lists(name, list_id) VALUES ($1,$2) "
                        "ON CONFLICT(name) DO UPDATE SET list_id=EXCLUDED.list_id",
                        name,
                        str(list_id),
                    )
                return str(list_id)

    created = await gtasks.create_tasklist(name)
    list_id = created.get("id")
    if not list_id:
        raise RuntimeError("Failed to create Google Tasks list")
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO g_tasks_lists(name, list_id) VALUES ($1,$2) "
            "ON CONFLICT(name) DO UPDATE SET list_id=EXCLUDED.list_id",
            name,
            str(list_id),
        )
    return str(list_id)


def due_from_local_date(dt_local: datetime | None, tz: ZoneInfo) -> datetime | None:
    """Convert a local deadline to a stable Google Tasks 'due' timestamp.

    Google Tasks stores an *instant* (RFC3339). If we send local midnight,
    it may show up as the previous day after timezone conversion. To keep the
    calendar *date* stable, we pin the time to 12:00 local and then convert to UTC.
    """
    if dt_local is None:
        return None
    if dt_local.tzinfo is None:
        dt_local = dt_local.replace(tzinfo=tz)
    local = dt_local.astimezone(tz)
    local_noon = local.replace(hour=12, minute=0, second=0, microsecond=0)
    return local_noon.astimezone(timezone.utc)
