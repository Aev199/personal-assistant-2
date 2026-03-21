"""Confirmation handlers for LLM drafts."""

from __future__ import annotations

import os

import asyncpg
from aiogram import Dispatcher, F
from aiogram.types import CallbackQuery

from bot.db import ensure_inbox_project_id
from bot.db.runtime_state import (
    get_pending_action,
    mark_pending_action_status,
    update_pending_action_payload,
)
from bot.deps import AppDeps
from bot.services.pending_actions import (
    _event_summary,
    _preview_keyboard,
    _preview_text,
    execute_pending_action,
)
from bot.ui import ui_render_home
from bot.ui.state import _ui_payload_get, ui_get_state, ui_payload_with_toast, ui_set_state
from bot.utils import try_delete_user_message


async def _toast_home(callback: CallbackQuery, db_pool: asyncpg.Pool, deps: AppDeps, text: str) -> None:
    async with db_pool.acquire() as conn:
        ui_state = await ui_get_state(conn, int(callback.message.chat.id))
        payload = _ui_payload_get(ui_state)
        payload = ui_payload_with_toast(payload, text, ttl_sec=25)
        await ui_set_state(conn, int(callback.message.chat.id), ui_payload=payload)
    await ui_render_home(callback.message, db_pool, tz_name=deps.tz_name, force_new=False)


async def cb_llm_confirm(callback: CallbackQuery, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    await callback.answer()
    try:
        pending_action_id = int((callback.data or "").split(":")[2])
    except Exception:
        return

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE pending_actions
            SET status='confirmed', confirmed_at=NOW()
            WHERE id=$1 AND chat_id=$2 AND status='pending'
            RETURNING id
            """,
            int(pending_action_id),
            int(callback.message.chat.id),
        )
        if row is None:
            pending = await get_pending_action(
                conn,
                chat_id=int(callback.message.chat.id),
                pending_action_id=int(pending_action_id),
            )
            if pending and pending.get("status") == "executed":
                await _toast_home(callback, db_pool, deps, "✅ Действие уже выполнено")
            else:
                await _toast_home(callback, db_pool, deps, "⏰ Черновик истёк. Отправьте запрос заново.")
            await try_delete_user_message(callback.message)
            return
        pending = await get_pending_action(
            conn,
            chat_id=int(callback.message.chat.id),
            pending_action_id=int(pending_action_id),
        )

    try:
        toast = await execute_pending_action(
            pending,
            db_pool=db_pool,
            deps=deps,
            chat_id=int(callback.message.chat.id),
        )
    except Exception as exc:
        async with db_pool.acquire() as conn:
            await mark_pending_action_status(
                conn,
                pending_action_id=int(pending_action_id),
                status="failed",
                last_error=str(exc),
            )
        await _toast_home(callback, db_pool, deps, f"Не удалось выполнить действие: {exc}")
        await try_delete_user_message(callback.message)
        return

    await _toast_home(callback, db_pool, deps, toast)
    await try_delete_user_message(callback.message)


async def cb_llm_cancel(callback: CallbackQuery, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    await callback.answer()
    try:
        pending_action_id = int((callback.data or "").split(":")[2])
    except Exception:
        return
    async with db_pool.acquire() as conn:
        await mark_pending_action_status(conn, pending_action_id=int(pending_action_id), status="cancelled")
    await _toast_home(callback, db_pool, deps, "✖ Черновик отменён")
    await try_delete_user_message(callback.message)


async def cb_llm_toggle_event_kind(callback: CallbackQuery, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    try:
        pending_action_id = int((callback.data or "").split(":")[2])
    except Exception:
        await callback.answer()
        return

    message = callback.message
    if message is None:
        await callback.answer()
        return

    async with db_pool.acquire() as conn:
        pending = await get_pending_action(
            conn,
            chat_id=int(message.chat.id),
            pending_action_id=int(pending_action_id),
        )
        if not pending or str(pending.get("kind") or "") != "event" or str(pending.get("status") or "") != "pending":
            await callback.answer("Черновик события недоступен", show_alert=False)
            return

        payload = dict(pending.get("payload") or {})
        title = str(payload.get("title") or "").strip()
        current_kind = str(payload.get("calendar_kind") or "personal").strip().lower()
        new_kind = "personal" if current_kind == "work" else "work"

        if new_kind == "work":
            calendar_url = os.getenv("ICLOUD_CALENDAR_URL_WORK", "").strip()
            if not calendar_url:
                await callback.answer("Не задан рабочий календарь", show_alert=True)
                return
            if payload.get("project_id"):
                project_code = await conn.fetchval("SELECT code FROM projects WHERE id=$1", int(payload["project_id"]))
                payload["project_code"] = str(project_code or "INBOX")
            else:
                inbox_project_id = await ensure_inbox_project_id(conn)
                payload["project_id"] = int(inbox_project_id)
                payload["project_code"] = "INBOX"
                payload["project_name"] = "INBOX"
            payload["calendar_kind"] = "work"
            payload["calendar_url"] = calendar_url
            payload["summary"] = _event_summary("work", title, payload.get("project_code") or "INBOX")
        else:
            calendar_url = os.getenv("ICLOUD_CALENDAR_URL_PERSONAL", "").strip()
            if not calendar_url:
                await callback.answer("Не задан личный календарь", show_alert=True)
                return
            payload["calendar_kind"] = "personal"
            payload["calendar_url"] = calendar_url
            for key in ("project_id", "project_code", "project_name"):
                payload.pop(key, None)
            payload["summary"] = _event_summary("personal", title, None)

        await update_pending_action_payload(conn, pending_action_id=int(pending_action_id), payload=payload)

    try:
        await message.edit_text(
            _preview_text("event", payload, tz_name=deps.tz_name),
            reply_markup=_preview_keyboard("event", int(pending_action_id), payload),
        )
    except Exception:
        await _toast_home(callback, db_pool, deps, "Переключил тип события, но не смог обновить черновик.")
        await callback.answer()
        return

    await callback.answer("Тип события переключён")


def register(dp: Dispatcher) -> None:
    dp.callback_query.register(cb_llm_confirm, F.data.startswith("llm:confirm:"))
    dp.callback_query.register(cb_llm_cancel, F.data.startswith("llm:cancel:"))
    dp.callback_query.register(cb_llm_toggle_event_kind, F.data.startswith("llm:toggle_event_kind:"))
