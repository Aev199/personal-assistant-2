"""Inbox (GTD) handlers.

Adds a lightweight "process inbox" flow (triage) without introducing new
wizard complexity. The flow is driven by inline callbacks and persists its
cursor in the SPA ui_payload.
"""

from __future__ import annotations

from datetime import datetime, timezone

import asyncpg
from aiogram import Dispatcher, F
from aiogram.types import CallbackQuery
from aiogram.fsm.context import FSMContext

from bot.deps import AppDeps
from bot.ui.screens import ui_render_home, ui_render_inbox
from bot.ui.state import ui_get_state, ui_set_state
from bot.ui.state import _ui_payload_get, _now_ts

from bot.handlers.tasks import show_task_card


def _utc_now_ts() -> int:
    return _now_ts()


async def cb_inbox_triage_start(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if deps.admin_id and callback.from_user and callback.from_user.id != deps.admin_id:
        return await callback.answer("Недоступно", show_alert=True)
    await callback.answer()
    await state.clear()

    if not callback.message:
        return
    chat_id = int(callback.message.chat.id)

    async with db_pool.acquire() as conn:
        ui_state = await ui_get_state(conn, chat_id)
        payload = _ui_payload_get(ui_state)

        inbox_id = await conn.fetchval("SELECT id FROM projects WHERE code='INBOX' LIMIT 1")
        if not inbox_id:
            payload["toast"] = {"text": "❌ Проект INBOX не найден", "exp": _utc_now_ts() + 20}
            await ui_set_state(conn, chat_id, ui_message_id=int(callback.message.message_id), ui_payload=payload)
            return await ui_render_home(callback.message, db_pool, tz_name=deps.tz_name)

        total = await conn.fetchval(
            "SELECT COUNT(*) FROM tasks WHERE status != 'done' AND project_id=$1",
            int(inbox_id),
        )
        total = int(total or 0)
        if total <= 0:
            payload["toast"] = {"text": "🎉 Inbox пуст", "exp": _utc_now_ts() + 20}
            await ui_set_state(conn, chat_id, ui_message_id=int(callback.message.message_id), ui_payload=payload)
            return await ui_render_inbox(callback.message, db_pool, tz_name=deps.tz_name, page=0)

        first = await conn.fetchrow(
            """
            SELECT id, created_at
            FROM tasks
            WHERE status != 'done' AND project_id=$1
            ORDER BY created_at ASC, id ASC
            LIMIT 1
            """,
            int(inbox_id),
        )
        if not first:
            payload["toast"] = {"text": "🎉 Inbox пуст", "exp": _utc_now_ts() + 20}
            await ui_set_state(conn, chat_id, ui_message_id=int(callback.message.message_id), ui_payload=payload)
            return await ui_render_inbox(callback.message, db_pool, tz_name=deps.tz_name, page=0)

        created_at: datetime = first["created_at"]
        payload["triage"] = {
            "active": True,
            "mode": "inbox",
            "inbox_id": int(inbox_id),
            "anchor_created_at": created_at.isoformat(),
            "anchor_id": int(first["id"]),
            # Return screen: if triage started from home, return home; else return inbox.
            "return": "home" if (ui_state.get("ui_screen") == "home") else "inbox",
        }

        await ui_set_state(
            conn,
            chat_id,
            ui_message_id=int(callback.message.message_id),
            ui_screen="inbox_triage",
            ui_payload=payload,
        )

        task_id = int(first["id"])

    await show_task_card(callback.message, db_pool, task_id, deps=deps)


async def cb_inbox_triage_next(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if deps.admin_id and callback.from_user and callback.from_user.id != deps.admin_id:
        return await callback.answer("Недоступно", show_alert=True)
    await callback.answer()
    await state.clear()
    if not callback.message:
        return
    chat_id = int(callback.message.chat.id)

    async with db_pool.acquire() as conn:
        ui_state = await ui_get_state(conn, chat_id)
        payload = _ui_payload_get(ui_state)
        triage = payload.get("triage") if isinstance(payload, dict) else None
        if not isinstance(triage, dict) or not triage.get("active"):
            payload["toast"] = {"text": "ℹ️ Разбор Inbox не активен", "exp": _utc_now_ts() + 15}
            await ui_set_state(conn, chat_id, ui_payload=payload)
            return await ui_render_home(callback.message, db_pool, tz_name=deps.tz_name)

        inbox_id = int(triage.get("inbox_id") or 0)
        if inbox_id <= 0:
            payload.pop("triage", None)
            payload["toast"] = {"text": "❌ Не найден INBOX", "exp": _utc_now_ts() + 20}
            await ui_set_state(conn, chat_id, ui_payload=payload)
            return await ui_render_home(callback.message, db_pool, tz_name=deps.tz_name)

        anchor_id = int(triage.get("anchor_id") or 0)
        anchor_created_at_raw = triage.get("anchor_created_at")
        try:
            anchor_created_at = datetime.fromisoformat(str(anchor_created_at_raw))
        except Exception:
            anchor_created_at = datetime.now(timezone.utc)

        nxt = await conn.fetchrow(
            """
            SELECT id, created_at
            FROM tasks
            WHERE status != 'done' AND project_id=$1
              AND (created_at > $2 OR (created_at = $2 AND id > $3))
            ORDER BY created_at ASC, id ASC
            LIMIT 1
            """,
            int(inbox_id),
            anchor_created_at,
            int(anchor_id),
        )

        if not nxt:
            # Finish triage
            payload.pop("triage", None)
            payload["toast"] = {"text": "🎉 Inbox разобран", "exp": _utc_now_ts() + 25}
            await ui_set_state(conn, chat_id, ui_payload=payload)
            # Return to where user started triage
            if triage.get("return") == "inbox":
                return await ui_render_inbox(callback.message, db_pool, tz_name=deps.tz_name, page=0)
            return await ui_render_home(callback.message, db_pool, tz_name=deps.tz_name)

        created_at: datetime = nxt["created_at"]
        triage["anchor_created_at"] = created_at.isoformat()
        triage["anchor_id"] = int(nxt["id"])
        payload["triage"] = triage

        await ui_set_state(conn, chat_id, ui_screen="inbox_triage", ui_payload=payload)
        task_id = int(nxt["id"])

    await show_task_card(callback.message, db_pool, task_id, deps=deps)


async def cb_inbox_triage_exit(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if deps.admin_id and callback.from_user and callback.from_user.id != deps.admin_id:
        return await callback.answer("Недоступно", show_alert=True)
    await callback.answer()
    await state.clear()
    if not callback.message:
        return
    chat_id = int(callback.message.chat.id)

    async with db_pool.acquire() as conn:
        ui_state = await ui_get_state(conn, chat_id)
        payload = _ui_payload_get(ui_state)
        triage = payload.get("triage") if isinstance(payload, dict) else None
        ret = "inbox"
        if isinstance(triage, dict) and triage.get("return"):
            ret = str(triage.get("return"))
        payload.pop("triage", None)
        await ui_set_state(conn, chat_id, ui_screen="inbox", ui_payload=payload)

    if ret == "home":
        return await ui_render_home(callback.message, db_pool, tz_name=deps.tz_name)
    return await ui_render_inbox(callback.message, db_pool, tz_name=deps.tz_name, page=0)


def register(dp: Dispatcher) -> None:
    dp.callback_query.register(cb_inbox_triage_start, F.data == "inbox:triage:start")
    dp.callback_query.register(cb_inbox_triage_next, F.data == "inbox:triage:next")
    dp.callback_query.register(cb_inbox_triage_exit, F.data == "inbox:triage:exit")
