"""Reminder message handlers."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import asyncpg
from aiogram import Dispatcher, F
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from bot.db.runtime_state import record_action_journal
from bot.deps import AppDeps
from bot.tz import to_db_utc
from bot.ui.state import ui_get_state, _ui_payload_get, ui_payload_with_toast, ui_set_state
from bot.utils import try_delete_user_message

logger = logging.getLogger(__name__)


def _reminders_page_from_data(data: str | None) -> int:
    try:
        parts = (data or "").split(":")
        if len(parts) >= 5 and parts[4].isdigit():
            return max(0, int(parts[4]))
    except Exception:
        return 0
    return 0


async def cb_rem_pick(callback: CallbackQuery, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    try:
        parts = (callback.data or "").split(":")
        page = max(0, int(parts[2]))
        rem_id = int(parts[3])
    except Exception:
        return await callback.answer("Ошибка", show_alert=True)

    await callback.answer()
    from bot.ui.screens import ui_render_reminders

    await ui_render_reminders(
        callback.message,
        db_pool,
        tz_name=deps.tz_name,
        page=page,
        selected_reminder_id=rem_id,
        preferred_message_id=callback.message.message_id,
    )


async def cb_rem_close(callback: CallbackQuery, deps: AppDeps) -> None:
    await callback.answer()
    await try_delete_user_message(callback.message)


async def cb_rem_snooze(callback: CallbackQuery, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    try:
        parts = (callback.data or "").split(":")
        mins = int(parts[2])
        rem_id = int(parts[3])
        page = max(0, int(parts[4])) if len(parts) >= 5 and parts[4].isdigit() else 0
        action_token = parts[5] if len(parts) >= 6 else f"page-{page}"
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

        ui_state = await ui_get_state(conn, int(callback.message.chat.id))
        payload = _ui_payload_get(ui_state)
        payload = ui_payload_with_toast(payload, f"⏸ Отложено на {mins} мин.", ttl_sec=5)
        payload.pop("selected_reminder_id", None)
        await ui_set_state(conn, int(callback.message.chat.id), ui_payload=payload)

    await callback.answer(f"⏸ Отложено на {mins} мин.")
    from bot.ui.screens import ui_render_reminders

    await ui_render_reminders(
        callback.message,
        db_pool,
        tz_name=deps.tz_name,
        page=page,
        selected_reminder_id=None,
        preferred_message_id=callback.message.message_id,
    )


async def cb_cancel_reminder(callback: CallbackQuery, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    rem_id = 0
    try:
        rem_id = int((callback.data or "").split(":")[2])
        page = _reminders_page_from_data(callback.data)
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE reminders
                SET status='cancelled',
                    cancelled_at_utc=NOW()
                WHERE id=$1 AND chat_id=$2 AND cancelled_at_utc IS NULL
                """,
                rem_id,
                int(callback.message.chat.id),
            )
            ui_state = await ui_get_state(conn, int(callback.message.chat.id))
            payload = _ui_payload_get(ui_state)
            payload = ui_payload_with_toast(payload, "✅ Напоминание удалено", ttl_sec=5)
            payload.pop("selected_reminder_id", None)
            await ui_set_state(conn, int(callback.message.chat.id), ui_payload=payload)
        await callback.answer("✅ Напоминание удалено")

        from bot.ui.screens import ui_render_reminders
        await ui_render_reminders(
            callback.message,
            db_pool,
            tz_name=deps.tz_name,
            page=page,
            selected_reminder_id=None,
            preferred_message_id=callback.message.message_id,
        )
    except Exception:
        logger.exception("Failed to cancel reminder", extra={"rem_id": rem_id})
        await callback.answer("Ошибка при удалении", show_alert=True)


async def cb_cancel_reminder_ask(callback: CallbackQuery, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    try:
        parts = (callback.data or "").split(":")
        rem_id = int(parts[2])
        page = max(0, int(parts[3])) if len(parts) >= 4 and parts[3].isdigit() else 0
    except Exception:
        return await callback.answer("Ошибка", show_alert=True)

    await callback.answer()
    from bot.ui.render import ui_render

    kb = [
        [InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"rem:cancel:{rem_id}:{page}")],
        [InlineKeyboardButton(text="⬅ К списку", callback_data=f"nav:reminders:{page}")],
    ]
    await ui_render(
        bot=callback.bot,
        db_pool=db_pool,
        chat_id=int(callback.message.chat.id),
        text="🗑 <b>Удалить напоминание?</b>\n\nДействие нельзя отменить.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
        screen="reminder_delete_confirm",
        payload={"reminders_page": page, "selected_reminder_id": rem_id},
        fallback_message=callback.message,
        preferred_message_id=callback.message.message_id,
        parse_mode="HTML",
    )


def register(dp: Dispatcher) -> None:
    dp.callback_query.register(cb_rem_close, F.data == "rem:close")
    dp.callback_query.register(cb_rem_pick, F.data.startswith("rem:pick:"))
    dp.callback_query.register(cb_rem_snooze, F.data.startswith("rem:snooze:"))
    dp.callback_query.register(cb_cancel_reminder_ask, F.data.startswith("rem:cancel_ask:"))
    dp.callback_query.register(cb_cancel_reminder, F.data.startswith("rem:cancel:"))
