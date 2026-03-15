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
) -> bool:
    """Send reminder with timeout and inline buttons; return True if sent."""

    token = (action_token or "").replace("-", "")[:16]
    snooze_15 = f"rem:snooze:15:{reminder_id}:{token}" if token else f"rem:snooze:15:{reminder_id}"
    snooze_60 = f"rem:snooze:60:{reminder_id}:{token}" if token else f"rem:snooze:60:{reminder_id}"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="OK", callback_data="rem:close"),
                InlineKeyboardButton(text="To task", callback_data=f"rem:task:{reminder_id}"),
            ],
            [
                InlineKeyboardButton(text="+15m", callback_data=snooze_15),
                InlineKeyboardButton(text="+1h", callback_data=snooze_60),
            ],
        ]
    )

    for attempt in range(3):
        try:
            await asyncio.wait_for(
                bot.send_message(
                    chat_id=chat_id,
                    text=f"Reminder:\n{text}",
                    reply_markup=kb,
                ),
                timeout=send_timeout_sec,
            )
            return True
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
            return False
    return False


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
