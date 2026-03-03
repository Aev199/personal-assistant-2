"""DB helpers: events/audit log."""

from __future__ import annotations

import asyncpg


async def db_add_event(
    conn: asyncpg.Connection,
    event_type: str,
    project_id: int | None,
    task_id: int | None,
    text: str,
) -> None:
    """Persist an event (history/audit) into DB."""
    await conn.execute(
        "INSERT INTO events (event_type, project_id, task_id, text) VALUES ($1,$2,$3,$4)",
        event_type,
        project_id,
        task_id,
        (text or "").strip(),
    )
