"""Reminder message handlers.

These are invoked from reminder notifications sent by /tick.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import asyncpg
from aiogram import Dispatcher, F
from aiogram.types import CallbackQuery

from bot.deps import AppDeps
from bot.tz import to_db_utc

from bot.utils import try_delete_user_message



async def cb_rem_close(callback: CallbackQuery, deps: AppDeps) -> None:
    if deps.admin_id and callback.from_user and callback.from_user.id != deps.admin_id:
        return
    await callback.answer()
    await try_delete_user_message(callback.message)


async def cb_rem_snooze(callback: CallbackQuery, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if deps.admin_id and callback.from_user and callback.from_user.id != deps.admin_id:
        return
    try:
        parts = callback.data.split(":")
        mins = int(parts[2])
        rem_id = int(parts[3])
    except Exception:
        return await callback.answer("Ошибка", show_alert=True)

    async with db_pool.acquire() as conn:
        text = await conn.fetchval("SELECT text FROM reminders WHERE id=$1", rem_id)
        if text:
            new_time = datetime.now(timezone.utc) + timedelta(minutes=mins)
            new_time_db = to_db_utc(
                new_time,
                tz_name=deps.tz_name,
                store_tz=bool(getattr(deps, 'db_reminders_remind_at_timestamptz', False)),
            )
            await conn.execute(
                "INSERT INTO reminders (text, remind_at, repeat) VALUES ($1, $2, 'none')",
                text,
                new_time_db,
            )

    await callback.answer(f"Отложено на {mins} мин.")
    await try_delete_user_message(callback.message)


def register(dp: Dispatcher) -> None:
    dp.callback_query.register(cb_rem_close, F.data == "rem:close")
    dp.callback_query.register(cb_rem_snooze, F.data.startswith("rem:snooze:"))
