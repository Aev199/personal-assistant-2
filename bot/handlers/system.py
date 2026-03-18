"""System and SPA glue handlers.

This module contains the remaining top-level commands, reply-keyboard routers,
and a few utility screens (sync status, Today pick/done, global tails).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from bot.tz import resolve_tz_name, resolve_tzinfo, to_local

import asyncpg
from bot.deps import AppDeps
from aiogram import Dispatcher, F
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.db import db_add_event, db_log_error
from bot.db.runtime_state import (
    clear_conversation_state,
    forget_recent_action,
    get_conversation_state,
    get_latest_undo_action,
    mark_action_undone,
)
from bot.db.projects import ensure_inbox_project_id
from bot.db.user_settings import get_current_project_id
from bot.fsm.states import FreeformFollowup
from bot.handlers.common import (
    cleanup_stale_wizard_message,
    get_wizard_message_data,
    split_wizard_message_target,
)
from bot.services.background import fire_and_forget
from bot.services.freeform_intake import handle_freeform_text, handle_freeform_voice
from bot.services.vault_sync import background_project_sync
from bot.ui import (
    ensure_main_menu,
    ui_render,
    ui_render_add_menu,
    ui_render_help,
    ui_render_home,
    ui_render_reminders,
    ui_get_state,
    ui_set_state,
)
from bot.ui.state import _ui_payload_get, ui_payload_get_undo, ui_payload_with_toast
from bot.utils import canon, fmt_msk, h, try_delete_user_message, fmt_task_line_html
from bot.ui.render import ui_safe_edit as safe_edit


UTC = ZoneInfo("UTC")


def _to_local(dt_utc_naive, tz_name: str):
    if dt_utc_naive is None:
        return None
    try:
        if getattr(dt_utc_naive, 'tzinfo', None) is None:
            dt_utc_naive = dt_utc_naive.replace(tzinfo=UTC)
        return dt_utc_naive.astimezone(ZoneInfo(tz_name))
    except Exception:
        return dt_utc_naive
from bot.keyboards import back_home_kb, main_menu_kb


async def _reply_wizard_context(
    state: FSMContext,
    *,
    fallback_chat_id: int,
) -> tuple[int | None, int | None, int | None]:
    wizard_chat_id, wizard_msg_id = await get_wizard_message_data(
        state,
        fallback_chat_id=fallback_chat_id,
    )
    preferred_message_id, stale_wizard_msg_id = split_wizard_message_target(
        wizard_msg_id,
        prefer_wizard=True,
    )
    return wizard_chat_id, preferred_message_id, stale_wizard_msg_id


def _clear_recent_fingerprint(payload: dict, fingerprint: str | None) -> dict:
    p = dict(payload or {})
    if not fingerprint:
        return p
    p["llm_recent"] = [
        item
        for item in (p.get("llm_recent") or [])
        if isinstance(item, dict) and str(item.get("fingerprint") or "") != str(fingerprint)
    ]
    return p


async def msg_undo_last(message: Message, state: FSMContext, deps: AppDeps, db_pool: asyncpg.Pool | None = None):
    if deps.admin_id and (not message.from_user or message.from_user.id != deps.admin_id):
        return

    await state.clear()
    if db_pool is None:
        return await message.answer("⚠️ Undo доступен только при подключённой БД.")

    await try_delete_user_message(message)

    chat_id = int(message.chat.id)
    work_project_id: int | None = None
    toast = "Нечего отменять."

    try:
        async with db_pool.acquire() as conn:
            ui_state = await ui_get_state(conn, chat_id)
            payload = _ui_payload_get(ui_state)
            journal = await get_latest_undo_action(conn, chat_id=chat_id)
            undo = dict((journal or {}).get("undo_payload") or {})
            if not undo:
                undo = ui_payload_get_undo(payload, undo_type="llm_create") or {}
            if not undo:
                payload.pop("undo", None)
                await ui_set_state(conn, chat_id, ui_payload=payload)
            else:
                action = str(undo.get("action") or "").strip().lower()
                fingerprint = str(undo.get("fingerprint") or "").strip() or None

                if action == "task":
                    task_id = int(undo.get("task_id") or 0)
                    row = await conn.fetchrow("SELECT project_id, title FROM tasks WHERE id=$1", task_id)
                    if row:
                        work_project_id = int(row["project_id"])
                        await conn.execute("DELETE FROM tasks WHERE id=$1", task_id)
                        await db_add_event(conn, "task_undo", work_project_id, None, f"↩️ Отмена создания задачи #{task_id} {row['title']}")
                        toast = f"↩️ Отменил задачу: {row['title']}"
                    else:
                        toast = "Задача уже отсутствует."
                elif action == "reminder":
                    reminder_id = int(undo.get("reminder_id") or 0)
                    row = await conn.fetchrow("SELECT text, status FROM reminders WHERE id=$1", reminder_id)
                    if row and str(row["status"] or "") not in {"sent", "cancelled"}:
                        await conn.execute(
                            """
                            UPDATE reminders
                            SET status='cancelled',
                                cancelled_at_utc=NOW(),
                                claim_token=NULL,
                                claimed_at_utc=NULL
                            WHERE id=$1
                            """,
                            reminder_id,
                        )
                        text = str(row["text"] or "")
                        await db_add_event(conn, "reminder_undo", None, None, f"↩️ Отмена напоминания: {text or reminder_id}")
                        toast = f"↩️ Напоминание отменено: {text or 'без текста'}"
                    elif row:
                        toast = "Напоминание уже отправлено или отменено."
                    else:
                        toast = "Напоминание уже отсутствует."
                elif action == "personal_task":
                    gtasks = getattr(deps, "gtasks", None)
                    list_id = str(undo.get("list_id") or "")
                    g_task_id = str(undo.get("g_task_id") or "")
                    title = str(undo.get("title") or "личное дело")
                    if gtasks is None or not gtasks.enabled() or not list_id or not g_task_id:
                        raise RuntimeError("Google Tasks undo unavailable")
                    await gtasks.delete_task(list_id, g_task_id)
                    await db_add_event(conn, "personal_task_undo", None, None, f"↩️ Отмена личной задачи: {title}")
                    toast = f"↩️ Личное дело отменено: {title}"
                elif action == "idea":
                    gtasks = getattr(deps, "gtasks", None)
                    list_id = str(undo.get("list_id") or "")
                    g_task_id = str(undo.get("g_task_id") or "")
                    title = str(undo.get("title") or "идея")
                    if gtasks is None or not gtasks.enabled() or not list_id or not g_task_id:
                        raise RuntimeError("Google Tasks undo unavailable")
                    await gtasks.delete_task(list_id, g_task_id)
                    await db_add_event(conn, "idea_undo", None, None, f"↩️ Отмена идеи: {title}")
                    toast = f"↩️ Идея отменена: {title}"
                elif action == "event":
                    icloud = getattr(deps, "icloud", None)
                    ics_url = str(undo.get("ics_url") or "")
                    calendar_url = str(undo.get("calendar_url") or "")
                    summary = str(undo.get("summary") or "событие")
                    dtstart_utc = str(undo.get("dtstart_utc") or "")
                    dtend_utc = str(undo.get("dtend_utc") or "")
                    work_project_id = int(undo.get("project_id") or 0) or None
                    if ics_url:
                        if icloud is None:
                            raise RuntimeError("iCloud undo unavailable")
                        ok = await icloud.delete_event(ics_url)
                        if not ok:
                            raise RuntimeError("Failed to delete iCloud event")
                    if ics_url:
                        await conn.execute("DELETE FROM icloud_events WHERE ics_url=$1", ics_url)
                    else:
                        await conn.execute(
                            "DELETE FROM icloud_events WHERE calendar_url=$1 AND summary=$2 AND dtstart_utc=$3::timestamptz AND dtend_utc=$4::timestamptz",
                            calendar_url,
                            summary,
                            dtstart_utc,
                            dtend_utc,
                        )
                    await db_add_event(conn, "ical_event_undo", work_project_id, None, f"↩️ Отмена события: {summary}")
                    toast = f"↩️ Событие отменено: {summary}"

                if journal and journal.get("id"):
                    await mark_action_undone(conn, int(journal["id"]))
                await forget_recent_action(conn, chat_id=chat_id, fingerprint=fingerprint)
                payload.pop("undo", None)
                payload = _clear_recent_fingerprint(payload, fingerprint)
                await ui_set_state(conn, chat_id, ui_payload=payload)
    except Exception as e:
        await db_log_error(db_pool, "msg_undo_last", e, {"chat_id": chat_id})
        toast = "Не удалось выполнить undo."

    if work_project_id:
        fire_and_forget(
            background_project_sync(int(work_project_id), db_pool, deps.vault, error_logger=lambda w, e, c: db_log_error(db_pool, w, e, c)),
            label="vault_sync",
        )

    await ui_render_home(message, db_pool, tz_name=resolve_tz_name(deps.tz_name), force_new=False)
    await ensure_main_menu(message, db_pool)
    try:
        async with db_pool.acquire() as conn:
            ui_state = await ui_get_state(conn, chat_id)
            payload = _ui_payload_get(ui_state)
            payload = ui_payload_with_toast(payload, toast, ttl_sec=25)
            await ui_set_state(conn, chat_id, ui_payload=payload)
    except Exception:
        pass
    await ui_render_home(message, db_pool, tz_name=resolve_tz_name(deps.tz_name), force_new=False)



async def cmd_start(message: Message, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps):
    if deps.admin_id and (not message.from_user or message.from_user.id != deps.admin_id):
        return
    wizard_chat_id, preferred_message_id, stale_wizard_msg_id = await _reply_wizard_context(
        state,
        fallback_chat_id=int(message.chat.id),
    )
    from bot.middlewares.fsm_persistence import recover_fsm_state
    recovered = await recover_fsm_state(int(message.chat.id), db_pool, state)
    
    if not recovered:
        await state.clear()
        
    await try_delete_user_message(message)
    anchor_sent = await ensure_main_menu(message, db_pool, recreate=True)
    
    if recovered:
        from bot.ui.render import ui_safe_wizard_render
        await ui_safe_wizard_render(
            bot=message.bot,
            state=state,
            chat_id=int(message.chat.id),
            fallback_msg=None,
            text="Вы вернулись к незавершенному черновику. Продолжите диалог или нажмите Отмена.",
            reply_markup=None,
        )
        return

    final_id = await ui_render_home(
        message,
        db_pool,
        tz_name=resolve_tz_name(deps.tz_name),
        preferred_message_id=preferred_message_id,
        force_new=bool(anchor_sent),
    )
    if final_id == 0:
        # Fallback: send a simple message if SPA rendering failed completely
        sent_message = await message.answer(
            "⚠️ Не удалось отобразить главный экран. Попробуйте позже.",
            reply_markup=back_home_kb(),
        )
        final_id = sent_message.message_id
    await cleanup_stale_wizard_message(
        message.bot,
        chat_id=wizard_chat_id,
        stale_message_id=stale_wizard_msg_id,
        final_message_id=final_id,
    )


async def cmd_menu(message: Message, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps):
    if deps.admin_id and (not message.from_user or message.from_user.id != deps.admin_id):
        return
    wizard_chat_id, preferred_message_id, stale_wizard_msg_id = await _reply_wizard_context(
        state,
        fallback_chat_id=int(message.chat.id),
    )
    from bot.middlewares.fsm_persistence import recover_fsm_state
    recovered = await recover_fsm_state(int(message.chat.id), db_pool, state)

    if not recovered:
        await state.clear()
        
    await try_delete_user_message(message)
    anchor_sent = await ensure_main_menu(message, db_pool, recreate=True)
    
    if recovered:
        from bot.ui.render import ui_safe_wizard_render
        await ui_safe_wizard_render(
            bot=message.bot,
            state=state,
            chat_id=int(message.chat.id),
            fallback_msg=None,
            text="Вы вернулись к незавершенному черновику. Продолжите диалог или нажмите Отмена.",
            reply_markup=None,
        )
        return

    final_id = await ui_render_home(
        message,
        db_pool,
        tz_name=resolve_tz_name(deps.tz_name),
        preferred_message_id=preferred_message_id,
        force_new=bool(anchor_sent),
    )
    if final_id == 0:
        # Fallback: send a simple message if SPA rendering failed completely
        sent_message = await message.answer(
            "⚠️ Не удалось отобразить главный экран. Попробуйте позже.",
            reply_markup=back_home_kb(),
        )
        final_id = sent_message.message_id
    await cleanup_stale_wizard_message(
        message.bot,
        chat_id=wizard_chat_id,
        stale_message_id=stale_wizard_msg_id,
        final_message_id=final_id,
    )


async def cmd_tz(message: Message, deps: AppDeps, db_pool: asyncpg.Pool | None = None):
    """Show runtime timezone diagnostics (admin-only)."""
    if deps.admin_id and (not message.from_user or message.from_user.id != deps.admin_id):
        return

    env_bot_tz = (os.getenv("BOT_TIMEZONE") or "").strip()
    env_tz = (os.getenv("TZ") or "").strip()
    dep_tz = (getattr(deps, "tz_name", "") or "").strip()

    resolved_name = resolve_tz_name(dep_tz or "Europe/Moscow")
    tzinfo = resolve_tzinfo(dep_tz or "Europe/Moscow")
    now_sys = datetime.now().astimezone()
    now_utc = datetime.now(timezone.utc)

    sample = datetime(2026, 3, 4, 15, 0, 0)  # naive UTC sample
    sample_local = to_local(sample, tzinfo)
    off = None
    try:
        off = tzinfo.utcoffset(now_sys) if tzinfo else None
    except Exception:
        off = None

    txt = (
        "🕰 <b>TZ debug</b>\n"
        f"BOT_TIMEZONE={env_bot_tz or '—'}\n"
        f"TZ={env_tz or '—'}\n"
        f"deps.tz_name={dep_tz or '—'}\n"
        f"resolve_tz_name(...)={resolved_name}\n"
        f"tzinfo={type(tzinfo).__name__} offset={off}\n\n"
        f"now_sys={now_sys.isoformat()}\n"
        f"now_utc={now_utc.isoformat()}\n\n"
        f"sample_utc_naive=2026-03-04 15:00 → local={sample_local.isoformat() if sample_local else '—'}\n"
    )

    # DB diagnostics (optional)
    if db_pool is not None:
        try:
            async with db_pool.acquire() as conn:
                db_tz = await conn.fetchval("SHOW TIME ZONE")
                cols = await conn.fetch(
                    "SELECT table_name, column_name, data_type "
                    "FROM information_schema.columns "
                    "WHERE table_schema='public' "
                    "AND ((table_name='tasks' AND column_name='deadline') "
                    "  OR (table_name='reminders' AND column_name='remind_at'))"
                )
            ct = {(r['table_name'], r['column_name']): (r['data_type'] or '') for r in cols}
            txt += (
                "\n<b>DB</b>\n"
                f"db_session_tz={h(str(db_tz or '—'))}\n"
                f"tasks.deadline={h(ct.get(('tasks','deadline'),'—'))}\n"
                f"reminders.remind_at={h(ct.get(('reminders','remind_at'),'—'))}\n"
                f"deps.db_tasks_deadline_timestamptz={getattr(deps,'db_tasks_deadline_timestamptz', False)}\n"
                f"deps.db_reminders_remind_at_timestamptz={getattr(deps,'db_reminders_remind_at_timestamptz', False)}\n"
            )
        except Exception:
            pass
    await message.answer(txt, parse_mode="HTML")


async def cmd_help(message: Message, state: FSMContext, deps: AppDeps, db_pool: asyncpg.Pool | None = None):
    if deps.admin_id and (not message.from_user or message.from_user.id != deps.admin_id):
        return
    wizard_chat_id = None
    preferred_message_id = None
    stale_wizard_msg_id = None
    if db_pool is not None:
        wizard_chat_id, preferred_message_id, stale_wizard_msg_id = await _reply_wizard_context(
            state,
            fallback_chat_id=int(message.chat.id),
        )
    await state.clear()

    if db_pool is not None:
        await try_delete_user_message(message)
        anchor_sent = await ensure_main_menu(message, db_pool)
        final_id = await ui_render_help(
            message,
            db_pool,
            preferred_message_id=preferred_message_id,
            force_new=bool(anchor_sent),
        )
        await cleanup_stale_wizard_message(
            message.bot,
            chat_id=wizard_chat_id,
            stale_message_id=stale_wizard_msg_id,
            final_message_id=final_id,
        )
        return

    help_text = (
        "🛠 Доступно (основной режим — кнопки внизу):\n\n"
        "Откройте экран Домой: /start\n"
        "Или нажмите ❓ Помощь внизу."
    )
    await message.answer(help_text, reply_markup=main_menu_kb())


async def cmd_add_menu(message: Message, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps):
    if deps.admin_id and (not message.from_user or message.from_user.id != deps.admin_id):
        return
    wizard_chat_id, preferred_message_id, stale_wizard_msg_id = await _reply_wizard_context(
        state,
        fallback_chat_id=int(message.chat.id),
    )
    await state.clear()
    await try_delete_user_message(message)
    anchor_sent = await ensure_main_menu(message, db_pool)
    final_id = await ui_render_add_menu(
        message,
        db_pool,
        preferred_message_id=preferred_message_id,
        force_new=bool(anchor_sent),
    )
    await cleanup_stale_wizard_message(
        message.bot,
        chat_id=wizard_chat_id,
        stale_message_id=stale_wizard_msg_id,
        final_message_id=final_id,
    )


async def cmd_help_button_router(message: Message, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps):
    if deps.admin_id and (not message.from_user or message.from_user.id != deps.admin_id):
        return
    wizard_chat_id, preferred_message_id, stale_wizard_msg_id = await _reply_wizard_context(
        state,
        fallback_chat_id=int(message.chat.id),
    )
    await state.clear()
    await try_delete_user_message(message)
    anchor_sent = await ensure_main_menu(message, db_pool)
    final_id = await ui_render_help(
        message,
        db_pool,
        preferred_message_id=preferred_message_id,
        force_new=bool(anchor_sent),
    )
    await cleanup_stale_wizard_message(
        message.bot,
        chat_id=wizard_chat_id,
        stale_message_id=stale_wizard_msg_id,
        final_message_id=final_id,
    )


async def cb_sync_status(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps):
    if not callback.from_user or callback.from_user.id != deps.admin_id:
        return await callback.answer("Недоступно", show_alert=True)
    await callback.answer()
    await state.clear()
    vault = deps.vault
    try:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT last_attempt_at, last_ok_at, last_error_at, last_error, last_duration_ms "
                "FROM sync_status WHERE name=$1",
                "vault",
            )

        status = "—"
        details = ""
        if row:
            ok_at = row["last_ok_at"]
            err_at = row["last_error_at"]
            err = (row["last_error"] or "").strip()
            if ok_at and (not err_at or ok_at >= err_at):
                status = f"✅ OK — {fmt_msk(ok_at)}"
            elif err_at:
                status = f"❌ Ошибка — {fmt_msk(err_at)}"
                if err:
                    details = f"\n\n<i>{h(err)}</i>"

        text = f"🔄 <b>Синхронизация</b>\n\nVault/Obsidian: <b>{h(status)}</b>{details}"
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔁 Повторить", callback_data="sync:retry")],
                [InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home")],
            ]
        )
        await ui_render(
            bot=callback.bot,
            db_pool=db_pool,
            chat_id=int(callback.message.chat.id),
            text=text,
            reply_markup=kb,
            screen="sync_status",
            payload={},
            fallback_message=callback.message,
            parse_mode="HTML",
        )
    except Exception as e:
        await safe_edit(callback.message, f"❌ Ошибка загрузки. Для фикса: {h(str(e))}", reply_markup=back_home_kb(), parse_mode="HTML")


async def cb_sync_retry(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps):
    if not callback.from_user or callback.from_user.id != deps.admin_id:
        return await callback.answer("Недоступно", show_alert=True)
    await callback.answer()
    await state.clear()
    vault = deps.vault

    try:
        chat_id = int(callback.message.chat.id)

        async with db_pool.acquire() as conn:
            pid = await get_current_project_id(conn, chat_id)
            if not pid:
                pid = await ensure_inbox_project_id(conn)

            ui_state = await ui_get_state(conn, chat_id)
            payload = _ui_payload_get(ui_state)
            payload = ui_payload_with_toast(payload, "🔄 Запустил синхронизацию…", ttl_sec=20)
            await ui_set_state(conn, chat_id, ui_payload=payload)

        vault = deps.vault
        if pid and vault:
            fire_and_forget(background_project_sync(int(pid), db_pool, vault), label=f"sync:retry:{pid}")
        await ui_render_home(callback.message, db_pool, tz_name=resolve_tz_name(deps.tz_name), force_new=False)
    except Exception as e:
        await safe_edit(callback.message, f"❌ Ошибка загрузки. Для фикса: {h(str(e))}", reply_markup=back_home_kb(), parse_mode="HTML")


async def cb_today_pick(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps):
    if not callback.from_user or callback.from_user.id != deps.admin_id:
        return await callback.answer("Недоступно", show_alert=True)
    await callback.answer()
    await state.clear()
    try:
        parts = (callback.data or "").split(":")
        page = 0
        if len(parts) >= 4 and parts[3].isdigit():
            page = max(0, int(parts[3]))
        from bot.ui import ui_render_today

        await ui_render_today(
            callback.message,
            db_pool,
            tz_name=resolve_tz_name(deps.tz_name),
            page=page,
        )
    except Exception as e:
        await safe_edit(callback.message, f"❌ Ошибка загрузки. Для фикса: {h(str(e))}", reply_markup=back_home_kb(), parse_mode="HTML")


async def cb_today_done(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps):
    if not callback.from_user or callback.from_user.id != deps.admin_id:
        return await callback.answer("Недоступно", show_alert=True)
    await callback.answer()
    await state.clear()
    try:
        tz_name = resolve_tz_name(deps.tz_name)
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT created_at, text
                FROM events
                WHERE event_type='task_done'
                  AND (created_at AT TIME ZONE $1)::date = (now() AT TIME ZONE $1)::date
                ORDER BY created_at DESC
                LIMIT 25
                """,
                tz_name,
            )
        lines = ["✅ СДЕЛАНО СЕГОДНЯ", ""]
        if not rows:
            lines.append("Пока ничего не закрыто.")
        else:
            for r in rows:
                lines.append(r["text"])
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="⬅ Сегодня", callback_data="nav:today"),
                    InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home"),
                ]
            ]
        )
        await ui_render(
            bot=callback.bot,
            db_pool=db_pool,
            chat_id=int(callback.message.chat.id),
            text="\n".join(lines),
            reply_markup=kb,
            screen="today_done",
            payload={},
            fallback_message=callback.message,
            parse_mode=None,
        )
    except Exception as e:
        await safe_edit(callback.message, f"❌ Ошибка загрузки. Для фикса: {h(str(e))}", reply_markup=back_home_kb(), parse_mode="HTML")


async def _render_global_tails_screen(msg: Message, db_pool: asyncpg.Pool, back_cb: str, deps: AppDeps):
    async with db_pool.acquire() as conn:
        nodate = await conn.fetchval("SELECT COUNT(*) FROM tasks WHERE status != 'done' AND kind != 'super' AND deadline IS NULL")
        postponed = await conn.fetchval("SELECT COUNT(*) FROM tasks WHERE status != 'done' AND kind != 'super' AND status='postponed'")

    parts = [
        "<b>🧺 ХВОСТЫ</b>",
        f"💤 Без срока: <b>{int(nodate or 0)}</b>",
        f"⏸ Отложено: <b>{int(postponed or 0)}</b>",
        "",
        "<i>Выберите список:</i>",
    ]
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💤 Без срока", callback_data=f"nav:tails_pick:nodate:0:{back_cb}")],
            [InlineKeyboardButton(text="⏸ Отложено", callback_data=f"nav:tails_pick:postponed:0:{back_cb}")],
            [
                InlineKeyboardButton(text="⬅ Назад", callback_data=back_cb),
                InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home"),
            ],
        ]
    )
    await ui_render(
        bot=msg.bot,
        db_pool=db_pool,
        chat_id=int(msg.chat.id),
        text="\n".join(parts),
        reply_markup=kb,
        screen="global_tails",
        payload={"back": back_cb},
        fallback_message=msg,
        parse_mode="HTML",
    )


async def cb_global_tails(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps):
    if not callback.from_user or callback.from_user.id != deps.admin_id:
        return await callback.answer("Недоступно", show_alert=True)
    await callback.answer()
    await state.clear()
    try:
        await _render_global_tails_screen(callback.message, db_pool, "nav:projects", deps)
    except Exception as e:
        await safe_edit(callback.message, f"❌ Ошибка загрузки. Для фикса: {h(str(e))}", reply_markup=back_home_kb(), parse_mode="HTML")


async def cb_global_tails_pick(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps):
    if not callback.from_user or callback.from_user.id != deps.admin_id:
        return await callback.answer("Недоступно", show_alert=True)
    await callback.answer()
    await state.clear()
    try:
        # nav:tails_pick:<kind>:<page>:<back_cb>
        parts = (callback.data or "").split(":")
        kind = parts[2] if len(parts) >= 3 else "nodate"
        page = int(parts[3]) if len(parts) >= 4 and parts[3].isdigit() else 0
        back_cb = ":".join(parts[4:]) if len(parts) >= 5 else "nav:projects"
        page = max(0, page)
        page_size = 8
        tz_name = resolve_tz_name(deps.tz_name)

        where = "t.deadline IS NULL AND t.status != 'postponed'" if kind == "nodate" else "t.status='postponed'"

        async with db_pool.acquire() as conn:
            total = await conn.fetchval(f"SELECT COUNT(*) FROM tasks t WHERE t.status != 'done' AND t.kind != 'super' AND {where}")
            rows = await conn.fetch(
                f"""
                SELECT t.id, t.title, p.code AS project, COALESCE(tm.name,'—') AS assignee, t.deadline
                FROM tasks t
                JOIN projects p ON t.project_id = p.id
                LEFT JOIN team tm ON t.assignee_id = tm.id
                WHERE t.status != 'done' AND t.kind != 'super' AND {where}
                ORDER BY p.code, t.id
                LIMIT $1 OFFSET $2
                """,
                page_size,
                page * page_size,
            )

        def _short(s: str, n: int = 34) -> str:
            s = (s or "").strip()
            return s if len(s) <= n else (s[: n - 1] + "…")

        title = "💤 БЕЗ СРОКА" if kind == "nodate" else "⏸ ОТЛОЖЕНО"
        lines = [f"<b>🧺 {h(title)}</b>"]
        if not rows:
            lines.append("Задач нет.")
        else:
            for r in rows:
                lines.append(
                    "• "
                    + fmt_task_line_html(
                        r.get("title") or "",
                        r.get("project") or "",
                        r.get("assignee") or "—",
                        (_to_local(r.get("deadline"), tz_name)),
                    )
                )

        kb: list[list[InlineKeyboardButton]] = []
        for r in rows:
            kb.append([InlineKeyboardButton(text=_short(r["title"], 30), callback_data=f"task:{r['id']}")])

        nav_row: list[InlineKeyboardButton] = []
        if page > 0:
            nav_row.append(InlineKeyboardButton(text="⬅️", callback_data=f"nav:tails_pick:{kind}:{page-1}:{back_cb}"))
        if (page + 1) * page_size < int(total or 0):
            nav_row.append(InlineKeyboardButton(text="➡️", callback_data=f"nav:tails_pick:{kind}:{page+1}:{back_cb}"))
        if nav_row:
            kb.append(nav_row)

        kb.append(
            [
                InlineKeyboardButton(text="⬅ Хвосты", callback_data="nav:global_tails"),
                InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home"),
            ]
        )

        await ui_render(
            bot=callback.bot,
            db_pool=db_pool,
            chat_id=int(callback.message.chat.id),
            text="\n".join(lines).strip(),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
            screen="tails_pick",
            payload={"kind": kind, "page": page, "back": back_cb},
            fallback_message=callback.message,
            parse_mode="HTML",
        )
    except Exception as e:
        await safe_edit(callback.message, f"❌ Ошибка загрузки. Для фикса: {h(str(e))}", reply_markup=back_home_kb(), parse_mode="HTML")


async def msg_projects_button(message: Message, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps):
    if deps.admin_id and (not message.from_user or message.from_user.id != deps.admin_id):
        return
    wizard_chat_id, preferred_message_id, stale_wizard_msg_id = await _reply_wizard_context(
        state,
        fallback_chat_id=int(message.chat.id),
    )
    await state.clear()
    await try_delete_user_message(message)
    from bot.ui import ui_render_projects_portfolio

    anchor_sent = await ensure_main_menu(message, db_pool)
    final_id = await ui_render_projects_portfolio(
        message,
        db_pool,
        tz_name=resolve_tz_name(deps.tz_name),
        preferred_message_id=preferred_message_id,
        force_new=bool(anchor_sent),
    )
    await cleanup_stale_wizard_message(
        message.bot,
        chat_id=wizard_chat_id,
        stale_message_id=stale_wizard_msg_id,
        final_message_id=final_id,
    )


async def msg_home_button(message: Message, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps):
    if deps.admin_id and (not message.from_user or message.from_user.id != deps.admin_id):
        return
    wizard_chat_id, preferred_message_id, stale_wizard_msg_id = await _reply_wizard_context(
        state,
        fallback_chat_id=int(message.chat.id),
    )
    await state.clear()
    await try_delete_user_message(message)
    anchor_sent = await ensure_main_menu(message, db_pool, recreate=True)
    final_id = await ui_render_home(
        message,
        db_pool,
        tz_name=resolve_tz_name(deps.tz_name),
        preferred_message_id=preferred_message_id,
        force_new=bool(anchor_sent),
    )
    if final_id == 0:
        # Fallback: send a simple message if SPA rendering failed completely
        sent_message = await message.answer(
            "⚠️ Не удалось отобразить главный экран. Попробуйте позже.",
            reply_markup=back_home_kb(),
        )
        final_id = sent_message.message_id
    await cleanup_stale_wizard_message(
        message.bot,
        chat_id=wizard_chat_id,
        stale_message_id=stale_wizard_msg_id,
        final_message_id=final_id,
    )


async def msg_today_button(message: Message, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps):
    if deps.admin_id and (not message.from_user or message.from_user.id != deps.admin_id):
        return
    wizard_chat_id, preferred_message_id, stale_wizard_msg_id = await _reply_wizard_context(
        state,
        fallback_chat_id=int(message.chat.id),
    )
    await state.clear()
    await try_delete_user_message(message)
    from bot.ui import ui_render_today

    anchor_sent = await ensure_main_menu(message, db_pool)
    final_id = await ui_render_today(
        message,
        db_pool,
        tz_name=resolve_tz_name(deps.tz_name),
        preferred_message_id=preferred_message_id,
        force_new=bool(anchor_sent),
    )
    await cleanup_stale_wizard_message(
        message.bot,
        chat_id=wizard_chat_id,
        stale_message_id=stale_wizard_msg_id,
        final_message_id=final_id,
    )


async def msg_overdue_button(message: Message, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps):
    if deps.admin_id and (not message.from_user or message.from_user.id != deps.admin_id):
        return
    wizard_chat_id, preferred_message_id, stale_wizard_msg_id = await _reply_wizard_context(
        state,
        fallback_chat_id=int(message.chat.id),
    )
    await state.clear()
    await try_delete_user_message(message)
    from bot.ui import ui_render_overdue

    anchor_sent = await ensure_main_menu(message, db_pool)
    final_id = await ui_render_overdue(
        message,
        db_pool,
        tz_name=resolve_tz_name(deps.tz_name),
        preferred_message_id=preferred_message_id,
        force_new=bool(anchor_sent),
    )
    await cleanup_stale_wizard_message(
        message.bot,
        chat_id=wizard_chat_id,
        stale_message_id=stale_wizard_msg_id,
        final_message_id=final_id,
    )


async def msg_reminders_button(message: Message, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps):
    if deps.admin_id and (not message.from_user or message.from_user.id != deps.admin_id):
        return
    wizard_chat_id, preferred_message_id, stale_wizard_msg_id = await _reply_wizard_context(
        state,
        fallback_chat_id=int(message.chat.id),
    )
    await state.clear()
    await try_delete_user_message(message)
    anchor_sent = await ensure_main_menu(message, db_pool)
    final_id = await ui_render_reminders(
        message,
        db_pool,
        page=0,
        selected_reminder_id=None,
        preferred_message_id=preferred_message_id,
        force_new=bool(anchor_sent),
    )
    await cleanup_stale_wizard_message(
        message.bot,
        chat_id=wizard_chat_id,
        stale_message_id=stale_wizard_msg_id,
        final_message_id=final_id,
    )


async def _freeform_followup_base_text(state: FSMContext, db_pool: asyncpg.Pool, chat_id: int) -> str:
    try:
        data = await state.get_data()
    except Exception:
        data = {}
    base_text = str((data or {}).get("freeform_base_text") or "").strip()
    if base_text:
        return base_text
    async with db_pool.acquire() as conn:
        persisted = await get_conversation_state(conn, chat_id, "freeform_followup")
    if not persisted:
        return ""
    payload = dict(persisted.get("payload") or {})
    return str(payload.get("freeform_base_text") or "").strip()


async def _freeform_followup_missing_context(
    message: Message,
    state: FSMContext,
    deps: AppDeps,
    db_pool: asyncpg.Pool,
) -> None:
    await state.clear()
    async with db_pool.acquire() as conn:
        await clear_conversation_state(conn, int(message.chat.id), "freeform_followup")
    try:
        async with db_pool.acquire() as conn:
            ui_state = await ui_get_state(conn, int(message.chat.id))
            payload = _ui_payload_get(ui_state)
            payload = ui_payload_with_toast(payload, "Контекст уточнения потерян. Повторите запрос целиком.", ttl_sec=25)
            await ui_set_state(conn, int(message.chat.id), ui_payload=payload)
    except Exception:
        pass
    await ui_render_home(message, db_pool, tz_name=resolve_tz_name(deps.tz_name), force_new=False)
    await ensure_main_menu(message, db_pool)


async def msg_freeform_followup_text(message: Message, state: FSMContext, deps: AppDeps, db_pool: asyncpg.Pool | None = None):
    if deps.admin_id and (not message.from_user or message.from_user.id != deps.admin_id):
        return

    if db_pool is None:
        await state.clear()
        return await message.answer("⚠️ Уточнение доступно только при подключённой БД и LLM.")

    base_text = await _freeform_followup_base_text(state, db_pool, int(message.chat.id))
    if not base_text:
        return await _freeform_followup_missing_context(message, state, deps, db_pool)

    await try_delete_user_message(message)

    handled = await handle_freeform_text(
        message,
        deps=deps,
        db_pool=db_pool,
        raw_text=message.text or "",
        source="text",
        state=state,
        prepend_text=base_text,
    )
    if handled:
        await ensure_main_menu(message, db_pool)
        return

    try:
        async with db_pool.acquire() as conn:
            ui_state = await ui_get_state(conn, int(message.chat.id))
            payload = _ui_payload_get(ui_state)
            payload = ui_payload_with_toast(payload, "Не удалось обработать уточнение. Попробуйте ещё раз.", ttl_sec=25)
            await ui_set_state(conn, int(message.chat.id), ui_payload=payload)
    except Exception:
        pass

    await ui_render_home(message, db_pool, tz_name=resolve_tz_name(deps.tz_name), force_new=False)
    await ensure_main_menu(message, db_pool)


async def msg_freeform_followup_voice(message: Message, state: FSMContext, deps: AppDeps, db_pool: asyncpg.Pool | None = None):
    if deps.admin_id and (not message.from_user or message.from_user.id != deps.admin_id):
        return

    if db_pool is None:
        await state.clear()
        return await message.answer("⚠️ Голосовые уточнения доступны только при подключённой БД и LLM.")

    base_text = await _freeform_followup_base_text(state, db_pool, int(message.chat.id))
    if not base_text:
        return await _freeform_followup_missing_context(message, state, deps, db_pool)

    await try_delete_user_message(message)

    handled = await handle_freeform_voice(
        message,
        deps=deps,
        db_pool=db_pool,
        state=state,
        prepend_text=base_text,
    )
    if handled:
        await ensure_main_menu(message, db_pool)
        return

    try:
        async with db_pool.acquire() as conn:
            ui_state = await ui_get_state(conn, int(message.chat.id))
            payload = _ui_payload_get(ui_state)
            payload = ui_payload_with_toast(payload, "Не удалось обработать голосовое уточнение. Попробуйте ещё раз.", ttl_sec=25)
            await ui_set_state(conn, int(message.chat.id), ui_payload=payload)
    except Exception:
        pass

    await ui_render_home(message, db_pool, tz_name=resolve_tz_name(deps.tz_name), force_new=False)
    await ensure_main_menu(message, db_pool)


async def cmd_unknown(message: Message, state: FSMContext, deps: AppDeps, db_pool: asyncpg.Pool | None = None):
    if deps.admin_id and (not message.from_user or message.from_user.id != deps.admin_id):
        return

    await state.clear()

    if db_pool is None:
        if not message.text:
            return await message.answer("⚠️ Я понимаю только текст. Нажмите ❓ Помощь.", reply_markup=main_menu_kb())
        if (message.text or "").strip().startswith("/"):
            return await message.answer("⚠️ Неизвестная команда. Нажмите ❓ Помощь.", reply_markup=main_menu_kb())
        return await message.answer(
            "🤔 Не понял. Нажмите ❓ Помощь или воспользуйтесь кнопками внизу.",
            reply_markup=main_menu_kb(),
        )

    await try_delete_user_message(message)

    raw = (message.text or "").strip()
    if raw and not raw.startswith("/"):
        handled = await handle_freeform_text(
            message,
            deps=deps,
            db_pool=db_pool,
            raw_text=raw,
            source="text",
            state=state,
        )
        if handled:
            await ensure_main_menu(message, db_pool)
            return
    if not raw:
        toast = "⚠️ Я понимаю только текст. Нажмите ❓ Помощь."
    elif raw.startswith("/"):
        toast = "⚠️ Неизвестная команда. Нажмите ❓ Помощь."
    else:
        toast = "⚠️ Не понял. Используйте ➕ Добавить или ⚡️ Быстрая задача."

    try:
        async with db_pool.acquire() as conn:
            ui_state = await ui_get_state(conn, int(message.chat.id))
            payload = _ui_payload_get(ui_state)
            payload = ui_payload_with_toast(payload, toast, ttl_sec=25)
            await ui_set_state(conn, int(message.chat.id), ui_payload=payload)
    except Exception:
        pass

    await ui_render_home(message, db_pool, tz_name=resolve_tz_name(deps.tz_name), force_new=False)
    await ensure_main_menu(message, db_pool)


async def msg_voice_freeform(message: Message, state: FSMContext, deps: AppDeps, db_pool: asyncpg.Pool | None = None):
    if deps.admin_id and (not message.from_user or message.from_user.id != deps.admin_id):
        return

    await state.clear()

    if db_pool is None:
        return await message.answer("⚠️ Голосовые сообщения доступны только при подключённой БД и LLM.")

    await try_delete_user_message(message)

    handled = await handle_freeform_voice(
        message,
        deps=deps,
        db_pool=db_pool,
        state=state,
    )
    if handled:
        await ensure_main_menu(message, db_pool)
        return

    try:
        async with db_pool.acquire() as conn:
            ui_state = await ui_get_state(conn, int(message.chat.id))
            payload = _ui_payload_get(ui_state)
            payload = ui_payload_with_toast(payload, "⚠️ Не удалось обработать голосовое сообщение.", ttl_sec=25)
            await ui_set_state(conn, int(message.chat.id), ui_payload=payload)
    except Exception:
        pass

    await ui_render_home(message, db_pool, tz_name=resolve_tz_name(deps.tz_name), force_new=False)
    await ensure_main_menu(message, db_pool)


def register(dp: Dispatcher) -> None:
    dp.message.register(cmd_start, CommandStart())
    dp.message.register(cmd_menu, Command("menu"))
    dp.message.register(cmd_tz, Command("tz"))
    dp.message.register(cmd_help, Command("help"))
    dp.message.register(msg_undo_last, StateFilter(None), lambda m: m.text and canon(m.text) in {"undo", "отмени", "отмени последнее"})
    dp.message.register(cmd_add_menu, lambda m: m.text and canon(m.text) in {"добавить", "➕ добавить"})
    dp.message.register(cmd_help_button_router, lambda m: m.text and canon(m.text) in {"help", "помощь"})

    dp.callback_query.register(cb_sync_status, F.data == "sync:status")
    dp.callback_query.register(cb_sync_retry, F.data == "sync:retry")

    dp.callback_query.register(cb_today_pick, F.data.startswith("nav:today:pick:"))
    dp.callback_query.register(cb_today_done, F.data == "nav:today:done")

    dp.callback_query.register(cb_global_tails, F.data == "nav:global_tails")
    dp.callback_query.register(cb_global_tails_pick, F.data.startswith("nav:tails_pick:"))
    dp.message.register(msg_home_button, lambda m: m.text and canon(m.text) == "домой")

    dp.message.register(msg_projects_button, lambda m: m.text and canon(m.text) == "проекты")
    dp.message.register(msg_today_button, lambda m: m.text and canon(m.text) == "сегодня")
    dp.message.register(msg_overdue_button, lambda m: m.text and canon(m.text) == "просрочки")
    dp.message.register(msg_reminders_button, lambda m: m.text and canon(m.text) == "напоминания")

    dp.message.register(msg_freeform_followup_voice, StateFilter(FreeformFollowup.awaiting_text), lambda m: bool(m.voice or m.audio))
    dp.message.register(msg_freeform_followup_text, StateFilter(FreeformFollowup.awaiting_text), F.text)
    dp.message.register(msg_voice_freeform, StateFilter(None), lambda m: bool(m.voice or m.audio))
    dp.message.register(cmd_unknown, StateFilter(None))
