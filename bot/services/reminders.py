"""Reminder services."""

from __future__ import annotations

import asyncio
import calendar
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.services.logger import get_logger
from bot.utils import h


log = get_logger("bot.services.reminders")


def _to_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if getattr(dt, "tzinfo", None) is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _to_utc_naive(dt: datetime | None) -> datetime | None:
    d = _to_utc(dt)
    if d is None:
        return None
    return d.replace(tzinfo=None)


async def send_reminder(
    *,
    bot: Bot,
    chat_id: int,
    reminder_id: int,
    text: str,
    send_timeout_sec: float = 10.0,
    action_token: str = "",
) -> int | None:
    """Send reminder and return Telegram message_id when delivery succeeds."""

    token = (action_token or "").replace("-", "")[:16]
    snooze_15 = f"rem:snooze:15:{reminder_id}:{token}" if token else f"rem:snooze:15:{reminder_id}"
    snooze_1h = f"rem:snooze:60:{reminder_id}:{token}" if token else f"rem:snooze:60:{reminder_id}"
    snooze_18 = f"rem:snooze:at18:{reminder_id}:{token}" if token else f"rem:snooze:at18:{reminder_id}"
    snooze_tom = f"rem:snooze:tom:{reminder_id}:{token}" if token else f"rem:snooze:tom:{reminder_id}"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="ОК", callback_data="rem:close"),
                InlineKeyboardButton(text="📝 В задачу", callback_data=f"rem:task:{reminder_id}"),
            ],
            [
                InlineKeyboardButton(text="⏸ 15м", callback_data=snooze_15),
                InlineKeyboardButton(text="⏸ 1ч", callback_data=snooze_1h),
                InlineKeyboardButton(text="🕕 18:00", callback_data=snooze_18),
                InlineKeyboardButton(text="⏳ Завтра 9:00", callback_data=snooze_tom),
            ],
        ]
    )

    for attempt in range(3):
        try:
            message = await asyncio.wait_for(
                bot.send_message(
                    chat_id=chat_id,
                    text=f"🔔 Напоминание:\n{text}",
                    reply_markup=kb,
                ),
                timeout=send_timeout_sec,
            )
            return int(message.message_id)
        except TelegramRetryAfter as e:
            await asyncio.sleep(float(getattr(e, "retry_after", 1.0)) + 0.1)
        except Exception as e:
            log.error(
                "failed to send reminder",
                error=e,
                attempt=attempt + 1,
                reminder_id=reminder_id,
                chat_id=chat_id,
            )
            return None
    return None


async def mark_telegram_reminder_snoozed(
    *,
    bot: Bot,
    chat_id: int,
    message_id: int,
    text: str,
    label: str = "15 минут",
) -> None:
    """Best-effort: make a delivered Telegram reminder visibly inactive."""
    try:
        await bot.edit_message_text(
            chat_id=int(chat_id),
            message_id=int(message_id),
            text=f"🔕 Отложено на {label}\n{text}",
            reply_markup=None,
        )
        return
    except Exception:
        pass

    try:
        await bot.edit_message_reply_markup(
            chat_id=int(chat_id),
            message_id=int(message_id),
            reply_markup=None,
        )
    except Exception:
        pass


def next_repeat_time_utc_naive(remind_at_dt: datetime, repeat: str, *, tz_name: str) -> datetime | None:
    """Compute next remind_at as UTC-naive for DB storage."""

    base_utc = _to_utc(remind_at_dt)
    if base_utc is None:
        return None

    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")
    base_local = base_utc.astimezone(tz)

    repeat = (repeat or "none").strip().lower()

    if repeat == "daily":
        nxt_local = base_local + timedelta(days=1)
    elif repeat == "weekly":
        nxt_local = base_local + timedelta(days=7)
    elif repeat == "workdays":
        nxt_local = base_local + timedelta(days=1)
        while nxt_local.weekday() >= 5:
            nxt_local = nxt_local + timedelta(days=1)
    elif repeat == "monthly":
        y = base_local.year
        mo = base_local.month + 1
        if mo == 13:
            y += 1
            mo = 1
        last_day = calendar.monthrange(y, mo)[1]
        day = min(base_local.day, last_day)
        nxt_local = base_local.replace(year=y, month=mo, day=day)
    else:
        return None

    return _to_utc_naive(nxt_local)



async def reschedule_reminder(
    conn,
    *,
    reminder_id: int,
    new_time_utc: datetime,
    fallback_chat_id: int,
    tz_name: str,
    store_tz: bool,
) -> int | None:
    """Cancel one reminder and replace it with a single new pending reminder."""
    row = await conn.fetchrow(
        "SELECT text, chat_id FROM reminders WHERE id=$1 AND cancelled_at_utc IS NULL",
        int(reminder_id),
    )
    if not row:
        return None

    from bot.tz import to_db_utc

    new_time = _to_utc(new_time_utc)
    if new_time is None:
        return None
    new_time_db = to_db_utc(
        new_time,
        tz_name=tz_name,
        store_tz=bool(store_tz),
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
            int(reminder_id),
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
            int(row["chat_id"] or fallback_chat_id),
            str(row["text"] or ""),
            new_time_db,
            new_time,
        )

    return int(new_id)


async def snooze_reminder(
    conn,
    *,
    reminder_id: int,
    minutes: int,
    fallback_chat_id: int,
    tz_name: str,
    store_tz: bool,
) -> tuple[int, datetime, str] | None:
    """Move one active reminder forward by a fixed number of minutes."""
    minutes = max(1, min(24 * 60, int(minutes)))
    new_time = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    new_id = await reschedule_reminder(
        conn,
        reminder_id=reminder_id,
        new_time_utc=new_time,
        fallback_chat_id=fallback_chat_id,
        tz_name=tz_name,
        store_tz=store_tz,
    )
    if new_id is None:
        return None

    label = f"{minutes} мин" if minutes < 60 else (
        f"{minutes // 60} ч" if minutes % 60 == 0
        else f"{minutes // 60} ч {minutes % 60} мин"
    )
    return new_id, new_time, label
