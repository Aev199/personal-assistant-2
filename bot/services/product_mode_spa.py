"""SPA-safe delivery for useful proactive assistant messages.

Proactive cards use the same minimal product language as the daily UI: tasks and
today. Technical buckets such as Inbox remain internal implementation details.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import asyncpg
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import bot.services.product_mode as product_mode
from bot.services.onboarding_state import load_onboarding_status
from bot.tz import resolve_tz_name
from bot.ui.render import ui_render
from bot.utils import h


_installed = False


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default


def _habit_policy(
    *,
    now_local: datetime,
    snapshot: dict[str, Any],
    onboarding_completed: bool,
    active_internal: int,
) -> dict[str, bool | str]:
    """Return pure delivery decisions for easy regression testing."""
    weekend = now_local.weekday() >= 5
    weekend_enabled = product_mode._env_enabled("ASSISTANT_WEEKEND_BRIEF", False)
    evening_enabled = product_mode._env_enabled("ASSISTANT_EVENING_NUDGE", False)
    min_items = max(1, _env_int("ASSISTANT_MIN_ITEMS_FOR_HABITS", 5))
    inbox_threshold = max(1, _env_int("ASSISTANT_EVENING_INBOX_THRESHOLD", 3))

    ready = bool(onboarding_completed or int(active_internal or 0) >= min_items)
    actionable_morning = (
        int(snapshot.get("today") or 0)
        + int(snapshot.get("overdue") or 0)
        + int(snapshot.get("reminders") or 0)
    ) > 0
    morning_allowed = ready and actionable_morning and (not weekend or weekend_enabled)
    evening_allowed = (
        ready
        and evening_enabled
        and int(snapshot.get("inbox") or 0) >= inbox_threshold
        and (not weekend or weekend_enabled)
    )

    reason = "ready"
    if not ready:
        reason = "awaiting_onboarding"
    elif weekend and not weekend_enabled:
        reason = "weekend_suppressed"
    elif not actionable_morning:
        reason = "no_actionable_morning_content"

    return {
        "ready": ready,
        "morning_allowed": morning_allowed,
        "evening_allowed": evening_allowed,
        "reason": reason,
    }


def _focus_line(focus: dict[str, Any] | None, tz: ZoneInfo) -> str | None:
    if not focus:
        return None
    title = str(focus.get("title") or "").strip()
    if not title:
        return None
    project = str(focus.get("code") or "").strip()
    if project.upper() == "INBOX":
        project = ""
    deadline = focus.get("deadline")
    when = ""
    if deadline is not None:
        try:
            if getattr(deadline, "tzinfo", None) is None:
                from datetime import timezone

                deadline = deadline.replace(tzinfo=timezone.utc)
            when = deadline.astimezone(tz).strftime("%H:%M")
        except Exception:
            when = ""
    meta = " · ".join(value for value in (project, when) if value)
    suffix = f" · {h(meta)}" if meta else ""
    return f"🎯 {h(title)}{suffix}"


async def _replace_spa_anchor(
    pool: asyncpg.Pool,
    *,
    bot,
    chat_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup,
) -> int:
    """Send a notifying message and atomically promote it to the SPA anchor."""
    message_id = await ui_render(
        bot=bot,
        db_pool=pool,
        chat_id=int(chat_id),
        text=text,
        reply_markup=reply_markup,
        screen=None,
        payload=None,
        force_new=True,
        parse_mode="HTML",
    )
    if message_id <= 0:
        raise RuntimeError("failed to replace SPA anchor with proactive message")
    return int(message_id)


async def maybe_send_habit_messages_spa(
    pool: asyncpg.Pool,
    *,
    bot,
    chat_id: int,
    tz_name: str,
) -> dict[str, object]:
    """Deliver only useful habit messages without creating a second SPA card."""
    if not product_mode._env_enabled("ASSISTANT_HABIT_MESSAGES", True):
        return {"enabled": False, "morning": "disabled", "evening": "disabled"}

    resolved_tz_name = resolve_tz_name(tz_name)
    tz = ZoneInfo(resolved_tz_name)
    now_local = datetime.now(tz)
    morning_hour = product_mode._env_hour("ASSISTANT_MORNING_BRIEF_HOUR", 8)
    evening_hour = product_mode._env_hour("ASSISTANT_EVENING_NUDGE_HOUR", 19)
    if evening_hour <= morning_hour:
        evening_hour = min(23, morning_hour + 8)

    async with pool.acquire() as conn:
        snapshot = await product_mode._load_habit_snapshot(
            conn,
            chat_id=int(chat_id),
            tz_name=resolved_tz_name,
            today=now_local.date(),
        )
        onboarding = await load_onboarding_status(conn, int(chat_id))

    policy = _habit_policy(
        now_local=now_local,
        snapshot=snapshot,
        onboarding_completed=bool(onboarding["completed"]),
        active_internal=int(onboarding["active_internal"] or 0),
    )
    result: dict[str, object] = {
        "enabled": True,
        "morning": "not_due",
        "evening": "not_due",
        "policy": policy["reason"],
    }

    if not policy["ready"]:
        result["morning"] = "awaiting_onboarding"
        result["evening"] = "awaiting_onboarding"
        return result

    if policy["morning_allowed"] and product_mode._morning_due(
        now_local,
        last_sent=snapshot["last_morning"],
        morning_hour=morning_hour,
        evening_hour=evening_hour,
    ):
        lines = [
            "☀️ <b>Сегодня</b>",
            f"Дел на сегодня: <b>{snapshot['today']}</b> · напоминаний: <b>{snapshot['reminders']}</b>",
        ]
        if int(snapshot.get("overdue") or 0):
            lines.append(f"Просрочено: <b>{snapshot['overdue']}</b>")
        focus_line = _focus_line(snapshot.get("focus"), tz)
        if focus_line:
            lines.extend(["", focus_line])

        await _replace_spa_anchor(
            pool,
            bot=bot,
            chat_id=int(chat_id),
            text="\n".join(lines),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="Сегодня", callback_data="nav:today"),
                        InlineKeyboardButton(text="Дела", callback_data="nav:home"),
                    ]
                ]
            ),
        )
        async with pool.acquire() as conn:
            await product_mode._mark_habit_sent(
                conn,
                chat_id=int(chat_id),
                column="last_morning_brief_date",
                sent_date=now_local.date(),
            )
        result["morning"] = "sent"
    elif not policy["morning_allowed"]:
        result["morning"] = str(policy["reason"])

    if policy["evening_allowed"] and product_mode._evening_due(
        now_local,
        last_sent=snapshot["last_evening"],
        evening_hour=evening_hour,
    ):
        await _replace_spa_anchor(
            pool,
            bot=bot,
            chat_id=int(chat_id),
            text=(
                "🌙 <b>Есть дела без проекта</b>\n"
                f"Таких дел: <b>{snapshot['inbox']}</b>. Они уже есть в общем списке — разбирать их отдельно не обязательно."
            ),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="Открыть дела", callback_data="nav:home")]
                ]
            ),
        )
        async with pool.acquire() as conn:
            await product_mode._mark_habit_sent(
                conn,
                chat_id=int(chat_id),
                column="last_evening_nudge_date",
                sent_date=now_local.date(),
            )
        result["evening"] = "sent"
    elif not policy["evening_allowed"]:
        result["evening"] = "disabled_or_below_threshold"

    return result


def install_product_mode_spa() -> None:
    """Replace proactive delivery after the base product mode is installed."""
    global _installed
    if _installed:
        return
    product_mode.maybe_send_habit_messages = maybe_send_habit_messages_spa
    _installed = True
