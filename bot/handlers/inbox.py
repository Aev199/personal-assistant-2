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
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.deps import AppDeps
from bot.ui.screens import ui_render, ui_render_home, ui_render_inbox
from bot.ui.state import ui_get_state, ui_set_state
from bot.ui.state import _ui_payload_get, ui_payload_with_toast

from bot.db import ensure_inbox_project_id
from bot.db.events import db_add_event
from bot.services.background import fire_and_forget
from bot.services.vault_sync import background_project_sync
from bot.handlers.tasks import show_task_card


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
            payload = ui_payload_with_toast(payload, "❌ Проект INBOX не найден", ttl_sec=20)
            await ui_set_state(conn, chat_id, ui_message_id=int(callback.message.message_id), ui_payload=payload)
            return await ui_render_home(callback.message, db_pool, tz_name=deps.tz_name)

        total = await conn.fetchval(
            "SELECT COUNT(*) FROM tasks WHERE status != 'done' AND kind != 'super' AND project_id=$1",
            int(inbox_id),
        )
        total = int(total or 0)
        if total <= 0:
            payload = ui_payload_with_toast(payload, "🎉 Inbox пуст", ttl_sec=20)
            await ui_set_state(conn, chat_id, ui_message_id=int(callback.message.message_id), ui_payload=payload)
            return await ui_render_inbox(callback.message, db_pool, tz_name=deps.tz_name, page=0)

        first = await conn.fetchrow(
            """
            SELECT id, created_at
            FROM tasks
            WHERE status != 'done' AND kind != 'super' AND project_id=$1
            ORDER BY created_at ASC, id ASC
            LIMIT 1
            """,
            int(inbox_id),
        )
        if not first:
            payload = ui_payload_with_toast(payload, "🎉 Inbox пуст", ttl_sec=20)
            await ui_set_state(conn, chat_id, ui_message_id=int(callback.message.message_id), ui_payload=payload)
            return await ui_render_inbox(callback.message, db_pool, tz_name=deps.tz_name, page=0)

        created_at: datetime = first["created_at"]
        payload["triage"] = {
            "active": True,
            "mode": "inbox",
            "inbox_id": int(inbox_id),
            "anchor_created_at": created_at.isoformat(),
            "anchor_id": int(first["id"]),
            "total": total,
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
            payload = ui_payload_with_toast(payload, "ℹ️ Разбор Inbox не активен", ttl_sec=15)
            await ui_set_state(conn, chat_id, ui_payload=payload)
            return await ui_render_home(callback.message, db_pool, tz_name=deps.tz_name)

        inbox_id = int(triage.get("inbox_id") or 0)
        if inbox_id <= 0:
            payload.pop("triage", None)
            payload = ui_payload_with_toast(payload, "❌ Не найден INBOX", ttl_sec=20)
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
            WHERE status != 'done' AND kind != 'super' AND project_id=$1
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
            payload = ui_payload_with_toast(payload, "🎉 Inbox разобран", ttl_sec=25)
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


async def cb_inbox_batch_project(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if deps.admin_id and callback.from_user and callback.from_user.id != deps.admin_id:
        return await callback.answer("Недоступно", show_alert=True)
    await callback.answer()
    await state.clear()
    
    chat_id = int(callback.message.chat.id)
    async with db_pool.acquire() as conn:
        inbox_id = await ensure_inbox_project_id(conn)
        rows = await conn.fetch("SELECT id, code FROM projects WHERE status='active' AND id != $1 ORDER BY code", inbox_id)
        
    kb_rows: list[list[InlineKeyboardButton]] = []
    row_buf: list[InlineKeyboardButton] = []
    for r in rows:
        pid = int(r["id"])
        row_buf.append(InlineKeyboardButton(text=str(r["code"]), callback_data=f"inbox:batch_exec:{pid}"))
        if len(row_buf) == 2:
            kb_rows.append(row_buf)
            row_buf = []
    if row_buf:
        kb_rows.append(row_buf)
    kb_rows.append([
        InlineKeyboardButton(text="⬅ Назад", callback_data="nav:inbox:0"),
    ])
    
    await ui_render(
        bot=callback.message.bot,
        db_pool=db_pool,
        chat_id=chat_id,
        text="📁 <b>Выберите проект для перемещения всех входящих задач:</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
        screen="inbox_batch",
        parse_mode="HTML",
    )


async def cb_inbox_batch_exec(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if deps.admin_id and callback.from_user and callback.from_user.id != deps.admin_id:
        return await callback.answer("Недоступно", show_alert=True)
        
    parts = (callback.data or "").split(":")
    if len(parts) < 3 or not parts[2].isdigit():
        return await callback.answer()
        
    target_pid = int(parts[2])
    chat_id = int(callback.message.chat.id)
    
    async with db_pool.acquire() as conn:
        inbox_id = await ensure_inbox_project_id(conn)
        target_code = await conn.fetchval("SELECT code FROM projects WHERE id=$1", target_pid)
        
        updated = await conn.execute(
            "UPDATE tasks SET project_id=$1 WHERE project_id=$2 AND status != 'done' AND kind != 'super'", 
            target_pid, inbox_id
        )
        cnt = int(updated.split()[-1]) if updated.startswith("UPDATE") else 0
        if cnt > 0:
            await db_add_event(conn, "inbox_batch_move", target_pid, None, f"Inbox -> [{target_code}] ({cnt} задач)")
            
    fire_and_forget(
        background_project_sync(target_pid, db_pool, deps.vault, error_logger=getattr(deps, "db_log_error", None)),
        label="vault_sync",
    )
            
    await callback.answer(f"Перемещено {cnt} задач в {target_code}")
    await ui_render_inbox(callback.message, db_pool, tz_name=deps.tz_name, page=0)


def register(dp: Dispatcher) -> None:
    dp.callback_query.register(cb_inbox_triage_start, F.data == "inbox:triage:start")
    dp.callback_query.register(cb_inbox_triage_next, F.data == "inbox:triage:next")
    dp.callback_query.register(cb_inbox_triage_exit, F.data == "inbox:triage:exit")
    dp.callback_query.register(cb_inbox_batch_project, F.data == "inbox:batch:project")
    dp.callback_query.register(cb_inbox_batch_exec, F.data.startswith("inbox:batch_exec:"))
