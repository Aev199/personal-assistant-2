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

from bot.db.projects import ensure_inbox_project_id
from bot.db.user_settings import get_current_project_id
from bot.services.background import fire_and_forget
from bot.services.vault_sync import background_project_sync
from bot.ui import (
    ensure_main_menu,
    ui_render,
    ui_render_add_menu,
    ui_render_help,
    ui_render_home,
    ui_get_state,
    ui_set_state,
)
from bot.ui.state import _ui_payload_get, _now_ts
from bot.utils import canon, fmt_msk, h, safe_edit, try_delete_user_message, fmt_task_line_html


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


async def _cleanup_wizard_message_from_state(bot, state: FSMContext, *, fallback_chat_id: int | None = None) -> None:
    """Remove wizard prompt message if any (tracked in FSM data as wizard_msg_id).

    This is needed for reply-keyboard navigation: user presses a menu button, we render
    a new SPA screen, but the wizard prompt message would otherwise stay in the chat.
    """
    try:
        data = await state.get_data()
        wiz_chat_id = int(data.get("wizard_chat_id") or (fallback_chat_id or 0))
        wiz_msg_id = data.get("wizard_msg_id")
        if not wiz_chat_id or not wiz_msg_id:
            return
        await bot.delete_message(chat_id=wiz_chat_id, message_id=int(wiz_msg_id))
    except Exception:
        return



async def cmd_start(message: Message, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps):
    if deps.admin_id and (not message.from_user or message.from_user.id != deps.admin_id):
        return
    await _cleanup_wizard_message_from_state(message.bot, state, fallback_chat_id=int(message.chat.id))
    await state.clear()
    await try_delete_user_message(message)
    await ensure_main_menu(message, db_pool)
    await ui_render_home(message, db_pool, tz_name=resolve_tz_name(deps.tz_name), force_new=True)


async def cmd_menu(message: Message, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps):
    if deps.admin_id and (not message.from_user or message.from_user.id != deps.admin_id):
        return
    await _cleanup_wizard_message_from_state(message.bot, state, fallback_chat_id=int(message.chat.id))
    await state.clear()
    await try_delete_user_message(message)
    await ensure_main_menu(message, db_pool)


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
    await _cleanup_wizard_message_from_state(message.bot, state, fallback_chat_id=int(message.chat.id))
    await state.clear()

    if db_pool is not None:
        await try_delete_user_message(message)
        await ensure_main_menu(message, db_pool)
        return await ui_render_help(message, db_pool, force_new=True)

    help_text = (
        "🛠 Доступно (основной режим — кнопки внизу):\n\n"
        "Откройте главное меню: /start\n"
        "Или нажмите ❓ Help внизу."
    )
    await message.answer(help_text, reply_markup=main_menu_kb())


async def cmd_add_menu(message: Message, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps):
    if deps.admin_id and (not message.from_user or message.from_user.id != deps.admin_id):
        return
    await _cleanup_wizard_message_from_state(message.bot, state, fallback_chat_id=int(message.chat.id))
    await state.clear()
    await try_delete_user_message(message)
    await ui_render_add_menu(message, db_pool, force_new=True)


async def cmd_help_button_router(message: Message, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps):
    if deps.admin_id and (not message.from_user or message.from_user.id != deps.admin_id):
        return
    await _cleanup_wizard_message_from_state(message.bot, state, fallback_chat_id=int(message.chat.id))
    await state.clear()
    await try_delete_user_message(message)
    await ui_render_help(message, db_pool, force_new=True)


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
        await safe_edit(callback.message, f"❌ Ошибка: {h(str(e))}", reply_markup=back_home_kb(), parse_mode="HTML")


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
            payload["toast"] = {"text": "🔄 Запустил синхронизацию…", "exp": _now_ts() + 20}
            await ui_set_state(conn, chat_id, ui_payload=payload)

        vault = deps.vault
        if pid and vault:
            fire_and_forget(background_project_sync(int(pid), db_pool, vault), label=f"sync:retry:{pid}")
        await ui_render_home(callback.message, db_pool, tz_name=resolve_tz_name(deps.tz_name), force_new=False)
    except Exception as e:
        await safe_edit(callback.message, f"❌ Ошибка: {h(str(e))}", reply_markup=back_home_kb(), parse_mode="HTML")


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
        page_size = 8
        tz_name = resolve_tz_name(deps.tz_name)

        async with db_pool.acquire() as conn:
            total = await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM tasks t
                WHERE t.status NOT IN ('done', 'postponed')
                  AND t.kind != 'super'
                  AND t.deadline IS NOT NULL
                  AND (t.deadline AT TIME ZONE 'UTC' AT TIME ZONE $1)::date = (now() AT TIME ZONE $1)::date
                """,
                tz_name,
            )
            rows = await conn.fetch(
                """
                SELECT t.id, t.title, p.code as project, COALESCE(tm.name,'—') as assignee, t.deadline
                FROM tasks t
                JOIN projects p ON t.project_id = p.id
                LEFT JOIN team tm ON t.assignee_id = tm.id
                WHERE t.status NOT IN ('done', 'postponed')
                  AND t.kind != 'super'
                  AND t.deadline IS NOT NULL
                  AND (t.deadline AT TIME ZONE 'UTC' AT TIME ZONE $1)::date = (now() AT TIME ZONE $1)::date
                ORDER BY t.deadline ASC
                LIMIT $2 OFFSET $3
                """,
                tz_name,
                page_size,
                page * page_size,
            )

        def _short(s: str, n: int = 34) -> str:
            s = (s or "").strip()
            return s if len(s) <= n else (s[: n - 1] + "…")

        lines = ["<b>📌 ЗАДАЧИ НА СЕГОДНЯ</b>"]
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
            nav_row.append(InlineKeyboardButton(text="⬅️", callback_data=f"nav:today:pick:{page-1}"))
        if (page + 1) * page_size < int(total or 0):
            nav_row.append(InlineKeyboardButton(text="➡️", callback_data=f"nav:today:pick:{page+1}"))
        if nav_row:
            kb.append(nav_row)

        kb.append(
            [
                InlineKeyboardButton(text="⬅ Сегодня", callback_data="nav:today"),
                InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home"),
            ]
        )
        await safe_edit(
            callback.message,
            "\n".join(lines).strip(),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
            parse_mode="HTML",
        )
    except Exception as e:
        await safe_edit(callback.message, f"❌ Ошибка: {h(str(e))}", reply_markup=back_home_kb(), parse_mode="HTML")


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
        await safe_edit(callback.message, "\n".join(lines), reply_markup=kb)
    except Exception as e:
        await safe_edit(callback.message, f"❌ Ошибка: {h(str(e))}", reply_markup=back_home_kb(), parse_mode="HTML")


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
        await safe_edit(callback.message, f"❌ Ошибка: {h(str(e))}", reply_markup=back_home_kb(), parse_mode="HTML")


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
        await safe_edit(callback.message, f"❌ Ошибка: {h(str(e))}", reply_markup=back_home_kb(), parse_mode="HTML")


async def msg_projects_button(message: Message, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps):
    if deps.admin_id and (not message.from_user or message.from_user.id != deps.admin_id):
        return
    await _cleanup_wizard_message_from_state(message.bot, state, fallback_chat_id=int(message.chat.id))
    await state.clear()
    await try_delete_user_message(message)
    from bot.ui import ui_render_projects_portfolio

    await ui_render_projects_portfolio(message, db_pool, tz_name=resolve_tz_name(deps.tz_name), force_new=True)


async def msg_today_button(message: Message, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps):
    if deps.admin_id and (not message.from_user or message.from_user.id != deps.admin_id):
        return
    await _cleanup_wizard_message_from_state(message.bot, state, fallback_chat_id=int(message.chat.id))
    await state.clear()
    await try_delete_user_message(message)
    from bot.ui import ui_render_today

    await ui_render_today(message, db_pool, tz_name=resolve_tz_name(deps.tz_name), force_new=True)


async def msg_overdue_button(message: Message, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps):
    if deps.admin_id and (not message.from_user or message.from_user.id != deps.admin_id):
        return
    await _cleanup_wizard_message_from_state(message.bot, state, fallback_chat_id=int(message.chat.id))
    await state.clear()
    await try_delete_user_message(message)
    from bot.ui import ui_render_overdue

    await ui_render_overdue(message, db_pool, tz_name=resolve_tz_name(deps.tz_name), force_new=True)


async def cmd_unknown(message: Message, state: FSMContext, deps: AppDeps, db_pool: asyncpg.Pool | None = None):
    if deps.admin_id and (not message.from_user or message.from_user.id != deps.admin_id):
        return

    await state.clear()

    if db_pool is None:
        if not message.text:
            return await message.answer("⚠️ Я понимаю только текст. Нажмите ❓ Help.", reply_markup=main_menu_kb())
        if (message.text or "").strip().startswith("/"):
            return await message.answer("⚠️ Неизвестная команда. Нажмите ❓ Help.", reply_markup=main_menu_kb())
        return await message.answer(
            "🤔 Не понял. Нажмите ❓ Help или воспользуйтесь кнопками внизу.",
            reply_markup=main_menu_kb(),
        )

    await try_delete_user_message(message)
    await ensure_main_menu(message, db_pool)

    raw = (message.text or "").strip()
    if not raw:
        toast = "⚠️ Я понимаю только текст. Нажмите ❓ Help."
    elif raw.startswith("/"):
        toast = "⚠️ Неизвестная команда. Нажмите ❓ Help."
    else:
        toast = "⚠️ Не понял. Используйте ➕ Добавить или ⚡️ Быстрая задача."

    try:
        async with db_pool.acquire() as conn:
            ui_state = await ui_get_state(conn, int(message.chat.id))
            payload = _ui_payload_get(ui_state)
            payload["toast"] = {"text": toast, "exp": _now_ts() + 25}
            await ui_set_state(conn, int(message.chat.id), ui_payload=payload)
    except Exception:
        pass

    await ui_render_home(message, db_pool, tz_name=resolve_tz_name(deps.tz_name), force_new=False)


def register(dp: Dispatcher) -> None:
    dp.message.register(cmd_start, CommandStart())
    dp.message.register(cmd_menu, Command("menu"))
    dp.message.register(cmd_tz, Command("tz"))
    dp.message.register(cmd_help, Command("help"))
    dp.message.register(cmd_add_menu, lambda m: m.text and canon(m.text) in {"добавить", "➕ добавить"})
    dp.message.register(cmd_help_button_router, lambda m: m.text and canon(m.text) in {"help", "помощь"})

    dp.callback_query.register(cb_sync_status, F.data == "sync:status")
    dp.callback_query.register(cb_sync_retry, F.data == "sync:retry")

    dp.callback_query.register(cb_today_pick, F.data.startswith("nav:today:pick:"))
    dp.callback_query.register(cb_today_done, F.data == "nav:today:done")

    dp.callback_query.register(cb_global_tails, F.data == "nav:global_tails")
    dp.callback_query.register(cb_global_tails_pick, F.data.startswith("nav:tails_pick:"))

    dp.message.register(msg_projects_button, lambda m: m.text and canon(m.text) == "проекты")
    dp.message.register(msg_today_button, lambda m: m.text and canon(m.text) == "сегодня")
    dp.message.register(msg_overdue_button, lambda m: m.text and canon(m.text) == "просрочки")

    dp.message.register(cmd_unknown, StateFilter(None))
