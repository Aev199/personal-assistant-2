"""Persona-mode helpers for lead vs solo UX."""

from __future__ import annotations

import asyncpg


PERSONA_LEAD = "lead"
PERSONA_SOLO = "solo"
PERSONA_VALUES = {PERSONA_LEAD, PERSONA_SOLO}

TEAM_BLOCK_TOAST = "👤 Режим Solo скрывает командные экраны. Переключите режим через ⋯ Ещё."


def normalize_persona_mode(value: object) -> str:
    raw = str(value or "").strip().lower()
    if raw in PERSONA_VALUES:
        return raw
    return PERSONA_LEAD


def is_solo_mode(value: object) -> bool:
    return normalize_persona_mode(value) == PERSONA_SOLO


def persona_toggle_button_text(value: object) -> str:
    return "👥 Режим Lead" if is_solo_mode(value) else "👤 Режим Solo"


def persona_toggle_target(value: object) -> str:
    return PERSONA_LEAD if is_solo_mode(value) else PERSONA_SOLO


def persona_switch_toast(value: object) -> str:
    return "👤 Включён режим Solo" if is_solo_mode(value) else "👥 Включён режим Lead"


async def get_persona_mode_from_pool(db_pool: asyncpg.Pool, chat_id: int) -> str:
    from bot.db.user_settings import get_persona_mode

    try:
        acquire = getattr(db_pool, "acquire", None)
        if acquire is None:
            return PERSONA_LEAD
        async with db_pool.acquire() as conn:
            return await get_persona_mode(conn, int(chat_id))
    except Exception:
        return PERSONA_LEAD
