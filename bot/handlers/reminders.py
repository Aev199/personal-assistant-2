"""Reminder message handlers."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import asyncpg
from aiogram import Dispatcher, F
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from bot.db.runtime_state import record_action_journal
from bot.deps import AppDeps
from bot.services.reminders import reschedule_reminder
from bot.tz import to_db_utc, resolve_tz_name
from zoneinfo import ZoneInfo
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
        val = parts[2]
        rem_id = int(parts[3])
        from_alert_message = False
        page = 0
        action_token = ""
        if len(parts) >= 5:
            slot = str(parts[4] or "").strip()
            if slot.isdigit():
                page = max(0, int(slot))
                action_token = parts[5] if len(parts) >= 6 else f"page-{page}"
            else:
                from_alert_message = True
                action_token = slot
        if not action_token:
            action_token = "alert" if from_alert_message else f"page-{page}"
    except Exception:
        return await callback.answer("Ошибка", show_alert=True)

    async with db_pool.acquire() as conn:
        alert_row = None
        if from_alert_message:
            alert_row = await conn.fetchrow(
                """
                SELECT text, chat_id, COALESCE(repeat, 'none') AS repeat,
                       status, telegram_message_id, claim_token
                FROM reminders
                WHERE id=$1 AND cancelled_at_utc IS NULL
                """,
                rem_id,
            )
            if not alert_row:
                return await callback.answer("Это напоминание уже неактуально")

            callback_message_id = int(getattr(callback.message, "message_id", 0) or 0)
            stored_message_id = int(alert_row["telegram_message_id"] or 0)
            stored_token = str(alert_row["claim_token"] or "").replace("-", "")[:16]
            alert_is_current = (
                (callback_message_id > 0 and stored_message_id == callback_message_id)
                or (bool(stored_token) and stored_token == action_token)
            )
            if not alert_is_current:
                return await callback.answer("Это напоминание уже неактуально")

        action_key = (
            f"snooze-alert:{rem_id}:{action_token or int(getattr(callback.message, 'message_id', 0) or 0)}"
            if from_alert_message
            else f"snooze:{rem_id}:{val}:{action_token or 'no-token'}"
        )
        journal_id = await record_action_journal(
            conn,
            chat_id=int(callback.message.chat.id),
            source="callback",
            action_type="reminder_snooze",
            summary=f"reminder {rem_id} snooze {val}",
            action_key=action_key,
        )
        if journal_id is None:
            return await callback.answer("Уже обработано")

        now_utc = datetime.now(timezone.utc)
        tz = ZoneInfo(resolve_tz_name(deps.tz_name or "Europe/Moscow"))
        now_local = now_utc.astimezone(tz)
        if val == "tom":
            dt = now_local + timedelta(days=1)
            new_time_local = dt.replace(hour=9, minute=0, second=0, microsecond=0)
            new_time = new_time_local.astimezone(timezone.utc)
            snooze_text = "завтра 09:00"
        elif val == "at18":
            new_time_local = now_local.replace(hour=18, minute=0, second=0, microsecond=0)
            if new_time_local <= now_local:
                new_time_local = new_time_local + timedelta(days=1)
                snooze_text = "завтра 18:00"
            else:
                snooze_text = "сегодня 18:00"
            new_time = new_time_local.astimezone(timezone.utc)
        else:
            try:
                mins = int(val)
            except Exception:
                return await callback.answer("Неверный вариант отложки", show_alert=True)
            new_time = now_utc + timedelta(minutes=mins)
            if mins >= 60:
                hours = mins // 60
                snooze_text = f"{hours} ч" if mins % 60 == 0 else f"{hours} ч {mins % 60} мин"
            else:
                snooze_text = f"{mins} мин"
        repeat = str(alert_row["repeat"] if alert_row is not None else "none").strip().lower()
        if from_alert_message and repeat != "none":
            # Tick advances a repeating reminder immediately after delivery.
            # Snoozing the delivered popup must therefore create a one-off
            # retry for the current occurrence, not destroy the future series.
            new_time_db = to_db_utc(
                new_time,
                tz_name=deps.tz_name,
                store_tz=bool(getattr(deps, "db_reminders_remind_at_timestamptz", False)),
            )
            new_id = await conn.fetchval(
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
                RETURNING id
                """,
                int(alert_row["chat_id"] or callback.message.chat.id),
                str(alert_row["text"] or ""),
                new_time_db,
                new_time,
            )
        else:
            new_id = await reschedule_reminder(
                conn,
                reminder_id=rem_id,
                new_time_utc=new_time,
                fallback_chat_id=int(callback.message.chat.id),
                tz_name=deps.tz_name,
                store_tz=bool(getattr(deps, "db_reminders_remind_at_timestamptz", False)),
            )
        if new_id is None:
            return await callback.answer("Напоминание не найдено", show_alert=True)

        ui_state = await ui_get_state(conn, int(callback.message.chat.id))
        payload = _ui_payload_get(ui_state)
        payload = ui_payload_with_toast(payload, f"⏸ Отложено на {snooze_text}", ttl_sec=5)
        payload.pop("selected_reminder_id", None)
        await ui_set_state(conn, int(callback.message.chat.id), ui_payload=payload)

    await callback.answer(f"⏸ Отложено на {snooze_text}")
    from bot.handlers.nav import _rerender_current_screen

    final_id = await _rerender_current_screen(
        callback.message,
        db_pool,
        deps=deps,
        preferred_message_id=None,
    )

    # Reminder popup should not become the primary SPA surface.
    if from_alert_message:
        callback_msg_id = int(getattr(callback.message, "message_id", 0) or 0)
        if callback_msg_id and callback_msg_id != int(final_id or 0):
            await try_delete_user_message(callback.message)


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
        [
            InlineKeyboardButton(text="⬅ К списку", callback_data=f"nav:reminders:{page}"),
            InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home"),
        ],
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
