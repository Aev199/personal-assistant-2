"""SPA-safe delivery for proactive assistant messages.

Proactive messages still need to be newly sent so Telegram can notify the user.
After sending, the new message becomes the single SPA anchor and the previous
anchor is removed by :func:`bot.ui.render.ui_render`.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import asyncpg
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import bot.services.product_mode as product_mode
from bot.tz import resolve_tz_name
from bot.ui.render import ui_render


_installed = False


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
        # Preserve the current logical screen/payload.  Passing ``screen=None``
        # also avoids adding an unrelated breadcrumb to the proactive card.
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
    """Deliver habit messages without leaving a second permanent bot message."""

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

    result: dict[str, object] = {
        "enabled": True,
        "morning": "not_due",
        "evening": "not_due",
    }

    if product_mode._morning_due(
        now_local,
        last_sent=snapshot["last_morning"],
        morning_hour=morning_hour,
        evening_hour=evening_hour,
    ):
        lines = [
            "☀️ <b>Доброе утро</b>",
            f"Сегодня: <b>{snapshot['today']}</b> задач · <b>{snapshot['reminders']}</b> напоминаний",
            f"Просрочено: <b>{snapshot['overdue']}</b> · Inbox: <b>{snapshot['inbox']}</b>",
        ]
        focus_line = product_mode._focus_line(snapshot.get("focus"), tz)
        if focus_line:
            lines.extend(["", focus_line])
        else:
            lines.extend(["", "День пока свободен — можно быстро добавить первый фокус."])

        await _replace_spa_anchor(
            pool,
            bot=bot,
            chat_id=int(chat_id),
            text="\n".join(lines),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="📅 Открыть сегодня", callback_data="nav:today"),
                        InlineKeyboardButton(text="➕ Добавить", callback_data="nav:add"),
                    ],
                    [
                        InlineKeyboardButton(
                            text=f"📥 Inbox ({snapshot['inbox']})",
                            callback_data="nav:inbox:0",
                        )
                    ],
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

    if int(snapshot["inbox"] or 0) > 0 and product_mode._evening_due(
        now_local,
        last_sent=snapshot["last_evening"],
        evening_hour=evening_hour,
    ):
        await _replace_spa_anchor(
            pool,
            bot=bot,
            chat_id=int(chat_id),
            text=(
                "🌙 <b>Inbox ждёт разбора</b>\n"
                f"Осталось записей: <b>{snapshot['inbox']}</b>.\n"
                "Можно разобрать их по одной и закончить день с чистой головой."
            ),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🧹 Разобрать Inbox",
                            callback_data="inbox:triage:start",
                        )
                    ]
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

    return result


def install_product_mode_spa() -> None:
    """Replace proactive delivery after the base product mode is installed."""

    global _installed
    if _installed:
        return
    product_mode.maybe_send_habit_messages = maybe_send_habit_messages_spa
    _installed = True
