"""Small internal idea lifecycle shared by Telegram and native clients."""

from __future__ import annotations

import asyncpg

from bot.db import db_add_event, ensure_inbox_project_id


async def list_active_ideas(
    conn: asyncpg.Connection,
    *,
    chat_id: int,
    limit: int = 50,
    offset: int = 0,
):
    limit = max(1, min(200, int(limit)))
    offset = max(0, int(offset))
    total = int(
        await conn.fetchval(
            "SELECT COUNT(*) FROM ideas WHERE chat_id=$1 AND status='active'",
            int(chat_id),
        )
        or 0
    )
    rows = await conn.fetch(
        """
        SELECT id, text, source, created_at
        FROM ideas
        WHERE chat_id=$1 AND status='active'
        ORDER BY created_at DESC, id DESC
        LIMIT $2 OFFSET $3
        """,
        int(chat_id),
        limit,
        offset,
    )
    return total, rows


async def archive_idea(
    conn: asyncpg.Connection,
    *,
    chat_id: int,
    idea_id: int,
) -> str | None:
    row = await conn.fetchrow(
        """
        SELECT id, text
        FROM ideas
        WHERE id=$1 AND chat_id=$2 AND status='active'
        FOR UPDATE
        """,
        int(idea_id),
        int(chat_id),
    )
    if not row:
        return None

    text = str(row["text"] or "")
    await conn.execute(
        """
        UPDATE ideas
        SET status='archived', archived_at=NOW()
        WHERE id=$1 AND chat_id=$2
        """,
        int(idea_id),
        int(chat_id),
    )
    await db_add_event(conn, "idea_archived", None, None, f"Идея: {text}")
    return text


async def promote_idea(
    conn: asyncpg.Connection,
    *,
    chat_id: int,
    idea_id: int,
) -> tuple[int, str] | None:
    row = await conn.fetchrow(
        """
        SELECT id, text
        FROM ideas
        WHERE id=$1 AND chat_id=$2 AND status='active'
        FOR UPDATE
        """,
        int(idea_id),
        int(chat_id),
    )
    if not row:
        return None

    text = str(row["text"] or "").strip()
    if not text:
        return None

    inbox_id = await ensure_inbox_project_id(conn)
    task_id = await conn.fetchval(
        """
        INSERT INTO tasks (project_id, title, status, kind)
        VALUES ($1, $2, 'todo', 'task')
        RETURNING id
        """,
        int(inbox_id),
        text,
    )
    await conn.execute(
        """
        UPDATE ideas
        SET status='promoted', archived_at=NOW()
        WHERE id=$1 AND chat_id=$2
        """,
        int(idea_id),
        int(chat_id),
    )
    await db_add_event(
        conn,
        "idea_promoted",
        int(inbox_id),
        int(task_id),
        f"Идея → задача: {text}",
    )
    return int(task_id), text
