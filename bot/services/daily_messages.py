"""Weekday proactive messages, sent by the regular tick.

* 10:00 — «Что сегодня?»: one tap per task builds «На сегодня».
* 17:00 — a short Inbox nudge, off unless ASSISTANT_EVENING_NUDGE=1.

Weekends stay quiet unless ASSISTANT_WEEKEND_BRIEF=1. Each message is sent at
most once per day; a late tick still sends it before the next slot.
"""
from __future__ import annotations

import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

import asyncpg
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.tz import resolve_tz_name
from bot.ui.day_plan import ui_render_day_plan
from bot.ui.render import ui_render


def _env_enabled(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_hour(name: str, default: int) -> int:
    try:
        return min(23, max(0, int(os.getenv(name, str(default)))))
    except ValueError:
        return default


def message_hours() -> tuple[int, int]:
    morning = _env_hour("ASSISTANT_MORNING_BRIEF_HOUR", 10)
    evening = _env_hour("ASSISTANT_EVENING_NUDGE_HOUR", 17)
    if evening <= morning:
        evening = min(23, morning + 8)
    return morning, evening


def due_messages(
    now_local: datetime,
    *,
    last_morning: date | None,
    last_evening: date | None,
    active_tasks: int,
    inbox_tasks: int,
) -> dict[str, bool]:
    """Pure decision, easy to test."""
    morning_hour, evening_hour = message_hours()
    today = now_local.date()
    weekday_ok = now_local.weekday() < 5 or _env_enabled("ASSISTANT_WEEKEND_BRIEF", False)
    threshold = max(1, int(os.getenv("ASSISTANT_EVENING_INBOX_THRESHOLD", "3") or 3))
    return {
        "morning": (
            weekday_ok
            and active_tasks > 0
            and last_morning != today
            and morning_hour <= now_local.hour < evening_hour
        ),
        "evening": (
            weekday_ok
            and _env_enabled("ASSISTANT_EVENING_NUDGE", False)
            and inbox_tasks >= threshold
            and last_evening != today
            and now_local.hour >= evening_hour
        ),
    }


async def _state(conn: asyncpg.Connection, chat_id: int) -> dict:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS assistant_habit_state (
            chat_id BIGINT PRIMARY KEY,
            last_morning_brief_date DATE,
            last_evening_nudge_date DATE,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    await conn.execute(
        "INSERT INTO assistant_habit_state(chat_id) VALUES($1) ON CONFLICT(chat_id) DO NOTHING",
        int(chat_id),
    )
    row = await conn.fetchrow(
        """
        SELECT s.last_morning_brief_date, s.last_evening_nudge_date,
               (SELECT COUNT(*) FROM tasks WHERE status NOT IN ('done','postponed') AND kind != 'super') AS active,
               (SELECT COUNT(*) FROM tasks t JOIN projects p ON p.id=t.project_id
                 WHERE t.status NOT IN ('done','postponed') AND t.kind='task' AND UPPER(p.code)='INBOX') AS inbox
        FROM assistant_habit_state s WHERE s.chat_id=$1
        """,
        int(chat_id),
    )
    return dict(row)


async def _mark_sent(pool: asyncpg.Pool, chat_id: int, column: str, day: date) -> None:
    assert column in {"last_morning_brief_date", "last_evening_nudge_date"}
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE assistant_habit_state SET {column}=$2, updated_at=NOW() WHERE chat_id=$1",
            int(chat_id),
            day,
        )


async def maybe_send_daily_messages(pool: asyncpg.Pool, *, bot, chat_id: int, tz_name: str) -> dict[str, str]:
    if not _env_enabled("ASSISTANT_HABIT_MESSAGES", True) or not chat_id:
        return {"morning": "disabled", "evening": "disabled"}

    resolved = resolve_tz_name(tz_name)
    now_local = datetime.now(ZoneInfo(resolved))
    async with pool.acquire() as conn:
        state = await _state(conn, int(chat_id))
    due = due_messages(
        now_local,
        last_morning=state["last_morning_brief_date"],
        last_evening=state["last_evening_nudge_date"],
        active_tasks=int(state["active"] or 0),
        inbox_tasks=int(state["inbox"] or 0),
    )
    result = {"morning": "not_due", "evening": "not_due"}

    if due["morning"]:
        message_id = await ui_render_day_plan(
            bot=bot, db_pool=pool, chat_id=int(chat_id), tz_name=resolved, greeting=True, force_new=True,
        )
        if message_id > 0:
            await _mark_sent(pool, chat_id, "last_morning_brief_date", now_local.date())
            result["morning"] = "sent"

    if due["evening"]:
        message_id = await ui_render(
            bot=bot,
            db_pool=pool,
            chat_id=int(chat_id),
            text=f"🌙 Во Входящих записей: <b>{int(state['inbox'])}</b>. Их можно разобрать в списке дел.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Все дела", callback_data="nav:all:all:0")]]
            ),
            force_new=True,
            parse_mode="HTML",
        )
        if message_id > 0:
            await _mark_sent(pool, chat_id, "last_evening_nudge_date", now_local.date())
            result["evening"] = "sent"
    return result
