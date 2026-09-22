"""Minimal Telegram actions for the secondary Ideas surface."""

from __future__ import annotations

import asyncpg
from aiogram import Dispatcher, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from bot.deps import AppDeps
from bot.services.ideas import archive_idea, promote_idea
from bot.ui.simple_daily import ui_render_idea, ui_render_ideas


def _parts(data: str | None) -> tuple[int, str, int] | None:
    raw = (data or "").split(":")
    if len(raw) != 4 or raw[0] != "idea" or not raw[1].isdigit() or not raw[3].isdigit():
        return None
    action = raw[2]
    if action not in {"open", "promote", "archive"}:
        return None
    return int(raw[1]), action, max(0, int(raw[3]))


async def cb_idea(
    callback: CallbackQuery,
    state: FSMContext,
    db_pool: asyncpg.Pool,
    deps: AppDeps,
) -> None:
    if deps.admin_id and callback.from_user and callback.from_user.id != deps.admin_id:
        return await callback.answer("Недоступно", show_alert=True)

    parsed = _parts(callback.data)
    if parsed is None:
        return await callback.answer()

    idea_id, action, page = parsed
    await state.clear()

    if action == "open":
        await callback.answer()
        await ui_render_idea(
            callback.message,
            db_pool,
            idea_id=idea_id,
            page=page,
        )
        return

    chat_id = int(callback.message.chat.id)
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            if action == "promote":
                result = await promote_idea(conn, chat_id=chat_id, idea_id=idea_id)
            else:
                result = await archive_idea(conn, chat_id=chat_id, idea_id=idea_id)

    if result is None:
        toast = "Идея уже обработана."
    elif action == "promote":
        _task_id, _text = result
        toast = "Добавлено во Входящие."
    else:
        toast = "Идея отправлена в архив."

    await callback.answer()
    await ui_render_ideas(
        callback.message,
        db_pool,
        page=page,
        toast=toast,
    )


def register(dp: Dispatcher) -> None:
    dp.callback_query.register(
        cb_idea,
        F.data.regexp(r"^idea:\d+:(?:open|promote|archive):\d+$"),
    )
