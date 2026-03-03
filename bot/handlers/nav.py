"""Navigation handlers (SPA).

This module registers handlers for top-level navigation buttons:
Home / Projects / Today / Overdue / Add / Help.
"""

from __future__ import annotations

import asyncpg
from aiogram import Dispatcher, F
from aiogram.types import CallbackQuery
from aiogram.fsm.context import FSMContext

from bot.deps import AppDeps

from bot.ui.screens import (
    ui_render_home,
    ui_render_help,
    ui_render_add_menu,
    ui_render_projects_portfolio,
    ui_render_today,
    ui_render_overdue,
    ui_render_team,
)

from bot.ui.state import ui_set_state


async def _cleanup_wizard_message(callback: CallbackQuery, state: FSMContext) -> None:
    """Best-effort cleanup for wizard "screen" message.

    Many flows render a wizard screen as a separate message tracked in FSM data
    (wizard_msg_id). When the user leaves the flow via top-level navigation,
    we should remove that wizard message to keep chat clean.

    If the user pressed navigation on the wizard message itself, we keep it
    (it will be edited by the SPA renderer).
    """

    try:
        data = await state.get_data()
        wiz_chat_id = data.get("wizard_chat_id")
        wiz_msg_id = data.get("wizard_msg_id")
        if not wiz_chat_id or not wiz_msg_id:
            return

        current_msg_id = getattr(callback.message, "message_id", None)
        if current_msg_id and int(wiz_msg_id) == int(current_msg_id):
            return

        await callback.bot.delete_message(chat_id=int(wiz_chat_id), message_id=int(wiz_msg_id))
    except Exception:
        return


async def _adopt_callback_message_as_ui(callback: CallbackQuery, db_pool: asyncpg.Pool) -> None:
    """Make the message with the pressed inline button the current SPA UI message.

    Some flows send a separate message (task cards, wizards, bulk actions, etc.).
    If the user presses a top-level navigation button (e.g. "⬅️ Домой") on that
    message, we want to update *that same message* instead of editing an older
    stored UI message (which would leave the current one behind).
    """
    msg = callback.message
    if not msg:
        return
    chat_id = int(msg.chat.id)
    msg_id = int(msg.message_id)
    async with db_pool.acquire() as conn:
        # Screen/payload will be updated by the target renderer.
        await ui_set_state(conn, chat_id, ui_message_id=msg_id)



async def cb_nav_home(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if deps.admin_id and callback.from_user and callback.from_user.id != deps.admin_id:
        return await callback.answer("Недоступно", show_alert=True)
    await callback.answer()
    await _cleanup_wizard_message(callback, state)
    await state.clear()
    await _adopt_callback_message_as_ui(callback, db_pool)
    await ui_render_home(callback.message, db_pool, tz_name=deps.tz_name)


async def cb_nav_close_inline(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if deps.admin_id and callback.from_user and callback.from_user.id != deps.admin_id:
        return await callback.answer("Недоступно", show_alert=True)
    await callback.answer()
    await _cleanup_wizard_message(callback, state)
    await state.clear()
    await _adopt_callback_message_as_ui(callback, db_pool)
    await ui_render_home(callback.message, db_pool, tz_name=deps.tz_name)


async def cb_nav_projects(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if deps.admin_id and callback.from_user and callback.from_user.id != deps.admin_id:
        return await callback.answer("Недоступно", show_alert=True)
    await callback.answer()
    await _cleanup_wizard_message(callback, state)
    await state.clear()
    await _adopt_callback_message_as_ui(callback, db_pool)
    await ui_render_projects_portfolio(callback.message, db_pool, tz_name=deps.tz_name)


async def cb_nav_today(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if deps.admin_id and callback.from_user and callback.from_user.id != deps.admin_id:
        return await callback.answer("Недоступно", show_alert=True)
    await callback.answer()
    await _cleanup_wizard_message(callback, state)
    await state.clear()
    await _adopt_callback_message_as_ui(callback, db_pool)
    await ui_render_today(callback.message, db_pool, tz_name=deps.tz_name)


async def cb_nav_overdue(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if deps.admin_id and callback.from_user and callback.from_user.id != deps.admin_id:
        return await callback.answer("Недоступно", show_alert=True)
    await callback.answer()
    await _cleanup_wizard_message(callback, state)
    await state.clear()
    await _adopt_callback_message_as_ui(callback, db_pool)
    await ui_render_overdue(callback.message, db_pool, tz_name=deps.tz_name)


async def cb_nav_add(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if deps.admin_id and callback.from_user and callback.from_user.id != deps.admin_id:
        return await callback.answer("Недоступно", show_alert=True)
    await callback.answer()
    await _cleanup_wizard_message(callback, state)
    await state.clear()
    await _adopt_callback_message_as_ui(callback, db_pool)
    await ui_render_add_menu(callback.message, db_pool)


async def cb_nav_help(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if deps.admin_id and callback.from_user and callback.from_user.id != deps.admin_id:
        return await callback.answer("Недоступно", show_alert=True)
    await callback.answer()
    await _cleanup_wizard_message(callback, state)
    await state.clear()
    await _adopt_callback_message_as_ui(callback, db_pool)
    await ui_render_help(callback.message, db_pool)


async def cb_nav_team(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if deps.admin_id and callback.from_user and callback.from_user.id != deps.admin_id:
        return await callback.answer("Недоступно", show_alert=True)
    await callback.answer()
    await _cleanup_wizard_message(callback, state)
    await state.clear()
    await _adopt_callback_message_as_ui(callback, db_pool)
    await ui_render_team(callback.message, db_pool)


def register(dp: Dispatcher) -> None:
    """Register navigation handlers on the provided Dispatcher."""
    dp.callback_query.register(cb_nav_projects, F.data == "nav:projects")
    dp.callback_query.register(cb_nav_close_inline, F.data == "nav:close_inline")
    dp.callback_query.register(cb_nav_home, F.data == "nav:home")
    dp.callback_query.register(cb_nav_add, F.data == "nav:add")
    dp.callback_query.register(cb_nav_help, F.data == "nav:help")
    dp.callback_query.register(cb_nav_today, F.data == "nav:today")
    dp.callback_query.register(cb_nav_overdue, F.data.startswith("nav:overdue"))
    dp.callback_query.register(cb_nav_team, F.data == "nav:team")
