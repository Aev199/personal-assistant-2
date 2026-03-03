"""Project-related DB helpers."""

from __future__ import annotations

import asyncpg


async def ensure_inbox_project_id(conn: asyncpg.Connection) -> int:
    """Ensure special INBOX project exists and return its id."""
    pid = await conn.fetchval("SELECT id FROM projects WHERE code='INBOX' LIMIT 1")
    if pid:
        try:
            await conn.execute("UPDATE projects SET status='active' WHERE id=$1", int(pid))
        except Exception:
            pass
        return int(pid)

    # Create project if missing
    try:
        pid = await conn.fetchval(
            "INSERT INTO projects(code, name, status) VALUES('INBOX','Входящие','active') RETURNING id"
        )
    except Exception:
        try:
            pid = await conn.fetchval(
                "INSERT INTO projects(code, name) VALUES('INBOX','Входящие') RETURNING id"
            )
        except Exception:
            pid = await conn.fetchval("SELECT id FROM projects WHERE code='INBOX' LIMIT 1")

    if not pid:
        raise RuntimeError("Не удалось создать проект INBOX")

    try:
        await conn.execute("UPDATE projects SET status='active' WHERE id=$1", int(pid))
    except Exception:
        pass

    return int(pid)


async def fetch_portfolio_rows(conn: asyncpg.Connection):
    """Fetch active projects with counts for portfolio / pickers."""
    return await conn.fetch(
        """
        SELECT p.id, p.code, p.name,
               COUNT(t.id) FILTER (WHERE t.status != 'done') AS active_tasks,
               COUNT(t.id) FILTER (
                   WHERE t.status != 'done'
                     AND t.deadline IS NOT NULL
                     AND t.deadline < (NOW() AT TIME ZONE 'UTC')
               ) AS overdue_tasks
        FROM projects p
        LEFT JOIN tasks t ON t.project_id = p.id
        WHERE p.status = 'active'
        GROUP BY p.id
        """
    )
