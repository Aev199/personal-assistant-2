"""Telegram picker for «На сегодня»: one message, one tap per task."""
from __future__ import annotations

from datetime import date, timezone
from zoneinfo import ZoneInfo

import asyncpg
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.services.day_plan import load_candidates, load_plan, local_today
from bot.tz import resolve_tz_name
from bot.ui.render import ui_render
from bot.utils import h

PLAN_SOFT_LIMIT = 5
GREETING = "☀️ <b>Что сегодня?</b>"


def _short(value: object, limit: int = 40) -> str:
    text = " ".join(str(value or "").split()) or "Без названия"
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _when(deadline, tz: ZoneInfo, today: date) -> str:
    if deadline is None:
        return ""
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    local = deadline.astimezone(tz)
    if local.date() == today:
        return f"до {local:%H:%M}"
    if local.date() < today:
        return "просрочено"
    return f"{local:%d.%m}"


def plan_picker_view(
    plan: list[dict],
    candidates: list[dict],
    *,
    tz: ZoneInfo,
    today: date,
    greeting: bool = False,
) -> tuple[str, InlineKeyboardMarkup]:
    lines = [GREETING if greeting else "<b>На сегодня</b>"]
    if plan:
        if greeting:
            lines.extend(["", "<b>На сегодня</b>"])
        for number, row in enumerate(plan, 1):
            when = _when(row.get("deadline"), tz, today)
            lines.append(f"{number}. {h(_short(row.get('title'), 80))}" + (f" · {when}" if when else ""))
    else:
        lines.extend(["", "Пока ничего не выбрано."])

    offer = candidates if len(plan) < PLAN_SOFT_LIMIT else []
    if offer:
        lines.extend(["", "Нажмите, чтобы добавить:"])

    kb: list[list[InlineKeyboardButton]] = []
    for row in plan:
        kb.append([InlineKeyboardButton(text=f"✓ {_short(row.get('title'))}", callback_data=f"plan:del:{int(row['id'])}")])
    for row in offer:
        when = _when(row.get("deadline"), tz, today)
        label = f"＋ {_short(row.get('title'), 34)}" + (f" · {when}" if when else "")
        kb.append([InlineKeyboardButton(text=label, callback_data=f"plan:add:{int(row['id'])}")])
    kb.append([InlineKeyboardButton(text="Готово", callback_data="nav:today")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb)


async def ui_render_day_plan(
    *,
    bot,
    db_pool: asyncpg.Pool,
    chat_id: int,
    tz_name: str | None,
    greeting: bool = False,
    fallback_message: Message | None = None,
    preferred_message_id: int | None = None,
    force_new: bool = False,
) -> int:
    resolved = resolve_tz_name(tz_name)
    tz = ZoneInfo(resolved)
    today = local_today(resolved)
    async with db_pool.acquire() as conn:
        plan = await load_plan(conn, tz_name=resolved, today=today)
        candidates = await load_candidates(conn, tz_name=resolved, today=today)
    text, markup = plan_picker_view(plan, candidates, tz=tz, today=today, greeting=greeting)
    return await ui_render(
        bot=bot,
        db_pool=db_pool,
        chat_id=int(chat_id),
        text=text,
        reply_markup=markup,
        screen="day_plan",
        payload={"greeting": greeting},
        fallback_message=fallback_message,
        preferred_message_id=preferred_message_id,
        force_new=force_new,
        parse_mode="HTML",
    )
