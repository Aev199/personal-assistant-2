"""DB helpers: user settings."""

from __future__ import annotations

import asyncpg

from bot.persona import normalize_persona_mode


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


async def get_persona_mode(conn: asyncpg.Connection, chat_id: int) -> str:
    try:
        value = await conn.fetchval("SELECT persona_mode FROM user_settings WHERE chat_id=$1", chat_id)
    except Exception:
        return "lead"
    return normalize_persona_mode(value)


async def set_persona_mode(conn: asyncpg.Connection, chat_id: int, persona_mode: str) -> str:
    normalized = normalize_persona_mode(persona_mode)
    await conn.execute(
        "INSERT INTO user_settings(chat_id, persona_mode, updated_at) VALUES($1,$2,NOW()) "
        "ON CONFLICT(chat_id) DO UPDATE SET persona_mode=EXCLUDED.persona_mode, updated_at=NOW()",
        chat_id,
        normalized,
    )
    return normalized
