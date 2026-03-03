"""DB helpers: user settings."""

from __future__ import annotations

import asyncpg


async def get_current_project_id(conn: asyncpg.Connection, chat_id: int) -> int | None:
    try:
        return await conn.fetchval("SELECT current_project_id FROM user_settings WHERE chat_id=$1", chat_id)
    except Exception:
        return None


async def set_current_project_id(conn: asyncpg.Connection, chat_id: int, project_id: int | None) -> None:
    await conn.execute(
        "INSERT INTO user_settings(chat_id, current_project_id, updated_at) VALUES($1,$2,NOW()) "
        "ON CONFLICT(chat_id) DO UPDATE SET current_project_id=EXCLUDED.current_project_id, updated_at=NOW()",
        chat_id,
        project_id,
    )
