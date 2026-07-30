"""Persistence helpers shared by onboarding and proactive-message policy."""

from __future__ import annotations

from typing import Any

import asyncpg


async def ensure_onboarding_state(conn: asyncpg.Connection, chat_id: int) -> None:
    """Ensure the habit table and onboarding marker exist on old deployments."""
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS assistant_habit_state (
            chat_id BIGINT PRIMARY KEY,
            last_morning_brief_date DATE,
            last_evening_nudge_date DATE,
            onboarding_completed_at TIMESTAMPTZ,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    await conn.execute(
        "ALTER TABLE assistant_habit_state "
        "ADD COLUMN IF NOT EXISTS onboarding_completed_at TIMESTAMPTZ"
    )
    await conn.execute(
        """
        INSERT INTO assistant_habit_state(chat_id)
        VALUES($1)
        ON CONFLICT(chat_id) DO NOTHING
        """,
        int(chat_id),
    )


async def mark_onboarding_complete(conn: asyncpg.Connection, chat_id: int) -> None:
    await ensure_onboarding_state(conn, chat_id)
    await conn.execute(
        """
        UPDATE assistant_habit_state
        SET onboarding_completed_at=COALESCE(onboarding_completed_at, NOW()),
            updated_at=NOW()
        WHERE chat_id=$1
        """,
        int(chat_id),
    )


async def load_onboarding_status(conn: asyncpg.Connection, chat_id: int) -> dict[str, Any]:
    await ensure_onboarding_state(conn, chat_id)
    completed_at = await conn.fetchval(
        "SELECT onboarding_completed_at FROM assistant_habit_state WHERE chat_id=$1",
        int(chat_id),
    )
    active_internal = int(
        await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM tasks
            WHERE status != 'done' AND kind != 'super'
            """
        )
        or 0
    )
    return {
        "completed": completed_at is not None,
        "completed_at": completed_at,
        "active_internal": active_internal,
    }
