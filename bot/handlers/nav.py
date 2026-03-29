"""Navigation handlers (SPA).

This module registers handlers for top-level navigation buttons:
Home / Projects / Today / Overdue / Add / Help.
"""

from __future__ import annotations

import asyncpg
from aiogram import Dispatcher, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from bot.deps import AppDeps
from bot.db.user_settings import get_persona_mode, set_persona_mode
from bot.persona import is_solo_mode, normalize_persona_mode, persona_switch_toast
from bot.handlers.common import (
    cleanup_stale_wizard_message,
    get_wizard_message_data,
    split_wizard_message_target,
)
from bot.ui.screens import (
    ui_render_add_menu,
    ui_render_all_tasks,
    ui_render_help,
    ui_render_home,
    ui_render_home_more,
    ui_render_inbox,
    ui_render_overdue,
    ui_render_projects_portfolio,
    ui_render_reminders,
    ui_render_stats,
    ui_render_team,
    ui_render_today,
    ui_render_work,
    ensure_main_menu,
)
from bot.ui.state import ui_get_state, ui_payload_with_toast, _ui_payload_get, ui_set_state


def _parse_nav_all_callback(data: str | None) -> tuple[str, int]:
    """Parse nav:all callback variants with backward-compatible fallbacks."""
    valid_filters = {"all", "overdue", "today", "nodate"}
    filter_key = "all"
    page = 0
    try:
        parts = (data or "").split(":")
        token = parts[2] if len(parts) >= 3 else ""
        token_page = parts[3] if len(parts) >= 4 else ""
        if token.isdigit():
            page = int(token)
        elif token:
            parsed_filter = token.lower()
            filter_key = parsed_filter if parsed_filter in valid_filters else "all"
            if token_page.isdigit():
                page = int(token_page)
    except Exception:
        return "all", 0
    return filter_key, max(0, page)


async def _callback_wizard_context(
    callback: CallbackQuery,
    state: FSMContext,
) -> tuple[int | None, int | None, int | None]:
    wizard_chat_id, wizard_msg_id = await get_wizard_message_data(
        state,
        fallback_chat_id=int(callback.message.chat.id),
    )
    preferred_message_id, stale_wizard_msg_id = split_wizard_message_target(
        wizard_msg_id,
        current_message_id=getattr(callback.message, "message_id", None),
    )
    return wizard_chat_id, preferred_message_id, stale_wizard_msg_id


async def _rerender_current_screen(
    message,
    db_pool: asyncpg.Pool,
    deps: AppDeps,
    *,
    persona_mode: str | None = None,
    toast: str | None = None,
    preferred_message_id: int | None = None,
) -> int:
    chat_id = int(message.chat.id)
    async with db_pool.acquire() as conn:
        ui_state = await ui_get_state(conn, chat_id)
        screen = str(ui_state.get("ui_screen") or "home").lower()
        payload = _ui_payload_get(ui_state)
        if toast:
            payload = ui_payload_with_toast(payload, toast, ttl_sec=20)
        await ui_set_state(conn, chat_id, ui_payload=payload)
        if persona_mode is None:
            persona_mode = await get_persona_mode(conn, chat_id)

    tz_name = deps.tz_name
    if screen == "secondary":
        return await ui_render_home_more(message, db_pool, preferred_message_id=preferred_message_id, force_new=False)
    if screen == "help":
        return await ui_render_help(message, db_pool, preferred_message_id=preferred_message_id, force_new=False)
    if screen == "today":
        page = 0
        try:
            page = max(0, int(payload.get("page") or 0))
        except Exception:
            page = 0
        return await ui_render_today(
            message,
            db_pool,
            tz_name=tz_name,
            page=page,
            icloud=deps.icloud,
            preferred_message_id=preferred_message_id,
            force_new=False,
        )
    if screen == "all_tasks":
        page = max(0, int(payload.get("page") or 0))
        filter_key = str(payload.get("filter") or "all").strip().lower() or "all"
        quick_done = bool(payload.get("quick_done"))
        return await ui_render_all_tasks(
            message,
            db_pool,
            tz_name=tz_name,
            page=page,
            filter_key=filter_key,
            quick_done=quick_done,
            preferred_message_id=preferred_message_id,
            force_new=False,
        )
    if screen == "projects":
        return await ui_render_projects_portfolio(
            message,
            db_pool,
            tz_name=tz_name,
            preferred_message_id=preferred_message_id,
            force_new=False,
        )
    if screen == "reminders":
        page = max(0, int(payload.get("reminders_page") or 0))
        selected = int(payload.get("selected_reminder_id") or 0) or None
        return await ui_render_reminders(
            message,
            db_pool,
            tz_name=tz_name,
            page=page,
            selected_reminder_id=selected,
            preferred_message_id=preferred_message_id,
            force_new=False,
        )
    if screen == "work":
        page = max(0, int(payload.get("page") or 0))
        return await ui_render_work(
            message,
            db_pool,
            tz_name=tz_name,
            page=page,
            preferred_message_id=preferred_message_id,
            force_new=False,
        )
    if screen in {"inbox", "inbox_triage"}:
        page = max(0, int(payload.get("inbox_page", payload.get("page")) or 0))
        return await ui_render_inbox(
            message,
            db_pool,
            tz_name=tz_name,
            page=page,
            preferred_message_id=preferred_message_id,
            force_new=False,
        )
    if screen == "stats":
        return await ui_render_stats(
            message,
            db_pool,
            tz_name=tz_name,
            preferred_message_id=preferred_message_id,
        )
    if screen == "team":
        if is_solo_mode(persona_mode):
            return await ui_render_home_more(message, db_pool, preferred_message_id=preferred_message_id, force_new=False)
        return await ui_render_team(message, db_pool, preferred_message_id=preferred_message_id, force_new=False)
    if screen == "add":
        return await ui_render_add_menu(message, db_pool, preferred_message_id=preferred_message_id, force_new=False)
    return await ui_render_home(
        message,
        db_pool,
        tz_name=tz_name,
        preferred_message_id=preferred_message_id,
        force_new=False,
    )


async def cb_nav_home(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if deps.admin_id and callback.from_user and callback.from_user.id != deps.admin_id:
        return await callback.answer("Недоступно", show_alert=True)
    await callback.answer()
    wizard_chat_id, preferred_message_id, stale_wizard_msg_id = await _callback_wizard_context(callback, state)
    await state.clear()
    final_id = await ui_render_home(
        callback.message,
        db_pool,
        tz_name=deps.tz_name,
        preferred_message_id=preferred_message_id,
    )
    await cleanup_stale_wizard_message(
        callback.bot,
        chat_id=wizard_chat_id,
        stale_message_id=stale_wizard_msg_id,
        final_message_id=final_id,
    )


async def cb_nav_stats(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if deps.admin_id and callback.from_user and callback.from_user.id != deps.admin_id:
        return await callback.answer("Недоступно", show_alert=True)
    await callback.answer()
    wizard_chat_id, preferred_message_id, stale_wizard_msg_id = await _callback_wizard_context(callback, state)
    await state.clear()
    final_id = await ui_render_stats(
        callback.message,
        db_pool,
        tz_name=deps.tz_name,
        preferred_message_id=preferred_message_id,
    )
    await cleanup_stale_wizard_message(
        callback.bot,
        chat_id=wizard_chat_id,
        stale_message_id=stale_wizard_msg_id,
        final_message_id=final_id,
    )


async def cb_nav_home_more(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if deps.admin_id and callback.from_user and callback.from_user.id != deps.admin_id:
        return await callback.answer("Недоступно", show_alert=True)
    await callback.answer()
    wizard_chat_id, preferred_message_id, stale_wizard_msg_id = await _callback_wizard_context(callback, state)
    await state.clear()
    final_id = await ui_render_home_more(
        callback.message,
        db_pool,
        preferred_message_id=preferred_message_id,
    )
    await cleanup_stale_wizard_message(
        callback.bot,
        chat_id=wizard_chat_id,
        stale_message_id=stale_wizard_msg_id,
        final_message_id=final_id,
    )


async def cb_nav_secondary(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    return await cb_nav_home_more(callback, state, db_pool, deps)


async def cb_nav_close_inline(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if deps.admin_id and callback.from_user and callback.from_user.id != deps.admin_id:
        return await callback.answer("Недоступно", show_alert=True)
    await callback.answer()
    wizard_chat_id, preferred_message_id, stale_wizard_msg_id = await _callback_wizard_context(callback, state)
    await state.clear()
    final_id = await ui_render_home(
        callback.message,
        db_pool,
        tz_name=deps.tz_name,
        preferred_message_id=preferred_message_id,
    )
    await cleanup_stale_wizard_message(
        callback.bot,
        chat_id=wizard_chat_id,
        stale_message_id=stale_wizard_msg_id,
        final_message_id=final_id,
    )


async def cb_nav_projects(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if deps.admin_id and callback.from_user and callback.from_user.id != deps.admin_id:
        return await callback.answer("Недоступно", show_alert=True)
    await callback.answer()
    wizard_chat_id, preferred_message_id, stale_wizard_msg_id = await _callback_wizard_context(callback, state)
    await state.clear()
    final_id = await ui_render_projects_portfolio(
        callback.message,
        db_pool,
        tz_name=deps.tz_name,
        preferred_message_id=preferred_message_id,
    )
    await cleanup_stale_wizard_message(
        callback.bot,
        chat_id=wizard_chat_id,
        stale_message_id=stale_wizard_msg_id,
        final_message_id=final_id,
    )


async def cb_nav_all(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if deps.admin_id and callback.from_user and callback.from_user.id != deps.admin_id:
        return await callback.answer("Недоступно", show_alert=True)
    await callback.answer()
    wizard_chat_id, preferred_message_id, stale_wizard_msg_id = await _callback_wizard_context(callback, state)
    await state.clear()
    raw_data = str(callback.data or "")
    quick_done = False
    if raw_data.endswith(":qd1"):
        quick_done = True
        raw_data = raw_data[:-4]
    elif raw_data.endswith(":qd0"):
        raw_data = raw_data[:-4]
    filter_key, page = _parse_nav_all_callback(raw_data)
    final_id = await ui_render_all_tasks(
        callback.message,
        db_pool,
        tz_name=deps.tz_name,
        page=page,
        filter_key=filter_key,
        quick_done=quick_done,
        preferred_message_id=preferred_message_id,
    )
    await cleanup_stale_wizard_message(
        callback.bot,
        chat_id=wizard_chat_id,
        stale_message_id=stale_wizard_msg_id,
        final_message_id=final_id,
    )


async def cb_nav_today(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if deps.admin_id and callback.from_user and callback.from_user.id != deps.admin_id:
        return await callback.answer("Недоступно", show_alert=True)
    await callback.answer()
    wizard_chat_id, preferred_message_id, stale_wizard_msg_id = await _callback_wizard_context(callback, state)
    await state.clear()
    page = 0
    try:
        parts = (callback.data or "").split(":")
        if len(parts) >= 3 and parts[2].isdigit():
            page = max(0, int(parts[2]))
    except Exception:
        page = 0
    final_id = await ui_render_today(
        callback.message,
        db_pool,
        tz_name=deps.tz_name,
        page=page,
        icloud=deps.icloud,
        preferred_message_id=preferred_message_id,
    )
    await cleanup_stale_wizard_message(
        callback.bot,
        chat_id=wizard_chat_id,
        stale_message_id=stale_wizard_msg_id,
        final_message_id=final_id,
    )


async def cb_nav_overdue(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if deps.admin_id and callback.from_user and callback.from_user.id != deps.admin_id:
        return await callback.answer("Недоступно", show_alert=True)
    await callback.answer()
    wizard_chat_id, preferred_message_id, stale_wizard_msg_id = await _callback_wizard_context(callback, state)
    await state.clear()
    page = 0
    try:
        parts = (callback.data or "").split(":")
        if len(parts) >= 3 and parts[2].isdigit():
            page = int(parts[2])
    except Exception:
        page = 0
    final_id = await ui_render_overdue(
        callback.message,
        db_pool,
        tz_name=deps.tz_name,
        page=page,
        preferred_message_id=preferred_message_id,
    )
    await cleanup_stale_wizard_message(
        callback.bot,
        chat_id=wizard_chat_id,
        stale_message_id=stale_wizard_msg_id,
        final_message_id=final_id,
    )


async def cb_nav_work(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if deps.admin_id and callback.from_user and callback.from_user.id != deps.admin_id:
        return await callback.answer("Недоступно", show_alert=True)
    await callback.answer()
    wizard_chat_id, preferred_message_id, stale_wizard_msg_id = await _callback_wizard_context(callback, state)
    await state.clear()
    page = 0
    try:
        parts = (callback.data or "").split(":")
        if len(parts) >= 3 and parts[2].isdigit():
            page = int(parts[2])
    except Exception:
        page = 0
    final_id = await ui_render_work(
        callback.message,
        db_pool,
        tz_name=deps.tz_name,
        page=page,
        preferred_message_id=preferred_message_id,
    )
    await cleanup_stale_wizard_message(
        callback.bot,
        chat_id=wizard_chat_id,
        stale_message_id=stale_wizard_msg_id,
        final_message_id=final_id,
    )


async def cb_nav_inbox(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if deps.admin_id and callback.from_user and callback.from_user.id != deps.admin_id:
        return await callback.answer("Недоступно", show_alert=True)
    await callback.answer()
    wizard_chat_id, preferred_message_id, stale_wizard_msg_id = await _callback_wizard_context(callback, state)
    await state.clear()
    page = 0
    try:
        parts = (callback.data or "").split(":")
        if len(parts) >= 3 and parts[2].isdigit():
            page = int(parts[2])
    except Exception:
        page = 0
    final_id = await ui_render_inbox(
        callback.message,
        db_pool,
        tz_name=deps.tz_name,
        page=page,
        preferred_message_id=preferred_message_id,
    )
    await cleanup_stale_wizard_message(
        callback.bot,
        chat_id=wizard_chat_id,
        stale_message_id=stale_wizard_msg_id,
        final_message_id=final_id,
    )


async def cb_nav_add(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if deps.admin_id and callback.from_user and callback.from_user.id != deps.admin_id:
        return await callback.answer("Недоступно", show_alert=True)
    await callback.answer()
    wizard_chat_id, preferred_message_id, stale_wizard_msg_id = await _callback_wizard_context(callback, state)
    await state.clear()
    final_id = await ui_render_add_menu(
        callback.message,
        db_pool,
        preferred_message_id=preferred_message_id,
    )
    await cleanup_stale_wizard_message(
        callback.bot,
        chat_id=wizard_chat_id,
        stale_message_id=stale_wizard_msg_id,
        final_message_id=final_id,
    )


async def cb_nav_help(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if deps.admin_id and callback.from_user and callback.from_user.id != deps.admin_id:
        return await callback.answer("Недоступно", show_alert=True)
    await callback.answer()
    wizard_chat_id, preferred_message_id, stale_wizard_msg_id = await _callback_wizard_context(callback, state)
    await state.clear()
    final_id = await ui_render_help(
        callback.message,
        db_pool,
        preferred_message_id=preferred_message_id,
    )
    await cleanup_stale_wizard_message(
        callback.bot,
        chat_id=wizard_chat_id,
        stale_message_id=stale_wizard_msg_id,
        final_message_id=final_id,
    )


async def cb_nav_team(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if deps.admin_id and callback.from_user and callback.from_user.id != deps.admin_id:
        return await callback.answer("Недоступно", show_alert=True)
    await callback.answer()
    wizard_chat_id, preferred_message_id, stale_wizard_msg_id = await _callback_wizard_context(callback, state)
    await state.clear()
    final_id = await ui_render_team(
        callback.message,
        db_pool,
        preferred_message_id=preferred_message_id,
    )
    await cleanup_stale_wizard_message(
        callback.bot,
        chat_id=wizard_chat_id,
        stale_message_id=stale_wizard_msg_id,
        final_message_id=final_id,
    )


async def cb_settings_persona(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if deps.admin_id and callback.from_user and callback.from_user.id != deps.admin_id:
        return await callback.answer("Недоступно", show_alert=True)
    target = normalize_persona_mode((callback.data or "").split(":")[-1])
    await callback.answer()
    wizard_chat_id, preferred_message_id, stale_wizard_msg_id = await _callback_wizard_context(callback, state)
    await state.clear()
    async with db_pool.acquire() as conn:
        persona_mode = await set_persona_mode(conn, int(callback.message.chat.id), target)
    await ensure_main_menu(callback.message, db_pool, refresh=True)
    final_id = await _rerender_current_screen(
        callback.message,
        db_pool,
        deps,
        persona_mode=persona_mode,
        toast=persona_switch_toast(persona_mode),
        preferred_message_id=preferred_message_id,
    )
    await cleanup_stale_wizard_message(
        callback.bot,
        chat_id=wizard_chat_id,
        stale_message_id=stale_wizard_msg_id,
        final_message_id=final_id,
    )


async def cb_nav_reminders(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if deps.admin_id and callback.from_user and callback.from_user.id != deps.admin_id:
        return await callback.answer("Недоступно", show_alert=True)
    await callback.answer()
    page = 0
    try:
        parts = (callback.data or "").split(":")
        if len(parts) >= 3 and parts[2].isdigit():
            page = max(0, int(parts[2]))
    except Exception:
        page = 0
    wizard_chat_id, preferred_message_id, stale_wizard_msg_id = await _callback_wizard_context(callback, state)
    await state.clear()
    
    final_id = await ui_render_reminders(
        callback.message,
        db_pool,
        tz_name=deps.tz_name,
        page=page,
        selected_reminder_id=None,
        preferred_message_id=preferred_message_id,
    )
    await cleanup_stale_wizard_message(
        callback.bot,
        chat_id=wizard_chat_id,
        stale_message_id=stale_wizard_msg_id,
        final_message_id=final_id,
    )


def register(dp: Dispatcher) -> None:
    """Register navigation handlers on the provided Dispatcher."""
    dp.callback_query.register(cb_nav_projects, F.data == "nav:projects")
    dp.callback_query.register(cb_nav_all, F.data.startswith("nav:all"))
    dp.callback_query.register(cb_nav_close_inline, F.data == "nav:close_inline")
    dp.callback_query.register(cb_nav_home, F.data == "nav:home")
    dp.callback_query.register(cb_nav_home_more, F.data == "nav:home_more")
    dp.callback_query.register(cb_nav_secondary, F.data == "nav:secondary")
    dp.callback_query.register(cb_nav_stats, F.data == "home:stats")
    dp.callback_query.register(cb_nav_add, F.data == "nav:add")
    dp.callback_query.register(cb_nav_help, F.data == "nav:help")
    dp.callback_query.register(cb_nav_today, F.data.regexp(r"^nav:today(?::\d+)?$"))
    dp.callback_query.register(cb_nav_overdue, F.data.startswith("nav:overdue"))
    dp.callback_query.register(cb_nav_work, F.data.startswith("nav:work"))
    dp.callback_query.register(cb_nav_inbox, F.data.startswith("nav:inbox"))
    dp.callback_query.register(cb_nav_team, F.data == "nav:team")
    dp.callback_query.register(cb_nav_reminders, F.data.startswith("nav:reminders"))
    dp.callback_query.register(cb_settings_persona, F.data.regexp(r"^settings:persona:(lead|solo)$"))
