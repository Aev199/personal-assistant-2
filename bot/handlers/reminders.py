"""Reminder message handlers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import asyncpg
from aiogram import Dispatcher, F
from aiogram.types import CallbackQuery

from bot.db.runtime_state import record_action_journal
from bot.deps import AppDeps
from bot.tz import to_db_utc
from bot.utils import try_delete_user_message


async def cb_rem_close(callback: CallbackQuery, deps: AppDeps) -> None:
    await callback.answer()
    await try_delete_user_message(callback.message)


async def cb_rem_snooze(callback: CallbackQuery, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    try:
        parts = (callback.data or "").split(":")
        mins = int(parts[2])
        rem_id = int(parts[3])
        action_token = parts[4] if len(parts) >= 5 else ""
    except Exception:
        return await callback.answer("Ошибка", show_alert=True)

    action_key = f"snooze:{rem_id}:{mins}:{action_token or 'no-token'}"
    async with db_pool.acquire() as conn:
        journal_id = await record_action_journal(
            conn,
            chat_id=int(callback.message.chat.id),
            source="callback",
            action_type="reminder_snooze",
            summary=f"reminder {rem_id} +{mins}m",
            action_key=action_key,
        )
        if journal_id is None:
            return await callback.answer("Уже обработано")

        row = await conn.fetchrow(
            "SELECT text, chat_id FROM reminders WHERE id=$1",
            int(rem_id),
        )
        if not row:
            return await callback.answer("Напоминание не найдено", show_alert=True)

        new_time = datetime.now(timezone.utc) + timedelta(minutes=mins)
        new_time_db = to_db_utc(
            new_time,
            tz_name=deps.tz_name,
            store_tz=bool(getattr(deps, "db_reminders_remind_at_timestamptz", False)),
        )
        async with conn.transaction():
            await conn.execute(
                """
                UPDATE reminders
                SET status='cancelled',
                    cancelled_at_utc=NOW(),
                    claim_token=NULL,
                    claimed_at_utc=NULL
                WHERE id=$1
                """,
                int(rem_id),
            )
            await conn.execute(
                """
                INSERT INTO reminders (
                    chat_id,
                    text,
                    remind_at,
                    repeat,
                    status,
                    next_attempt_at_utc,
                    is_sent
                )
                VALUES ($1, $2, $3, 'none', 'pending', $4, FALSE)
                """,
                int(row["chat_id"] or callback.message.chat.id),
                str(row["text"] or ""),
                new_time_db,
                new_time,
            )

    await callback.answer(f"⏸ Отложено на {mins} мин.")
    await try_delete_user_message(callback.message)


async def cb_cancel_reminder(callback: CallbackQuery, db_pool: asyncpg.Pool) -> None:
    rem_id = callback.data.split(":")[2]
    try:
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE reminders
                SET status='cancelled',
                    cancelled_at_utc=NOW()
                WHERE id=$1 AND chat_id=$2 AND cancelled_at_utc IS NULL
                """,
                int(rem_id),
                int(callback.message.chat.id),
            )
        await callback.answer("✅ Напоминание удалено")
        
        # We need to refresh the reminders list screen
        from bot.ui.screens import ui_render_reminders
        await ui_render_reminders(
            callback.message,
            db_pool,
            preferred_message_id=callback.message.message_id,
        )
    except Exception as e:
        logger.exception("Failed to cancel reminder", extra={"rem_id": rem_id})
        await callback.answer("Ошибка при удалении", show_alert=True)


def register(dp: Dispatcher) -> None:
    dp.callback_query.register(cb_rem_close, F.data == "rem:close")
    dp.callback_query.register(cb_rem_snooze, F.data.startswith("rem:snooze:"))
    dp.callback_query.register(cb_cancel_reminder, F.data.startswith("rem:cancel:"))
