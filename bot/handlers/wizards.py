"""Wizard flows (task, reminder, personal tasks, iCloud events, quick capture).

This module extracts wizard handlers from the monolith while keeping callback_data
and FSM data schema stable.
"""

from __future__ import annotations

import asyncio
import os
import re
import calendar
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from bot.tz import resolve_tz_name
from bot.tz import to_db_utc

import asyncpg
import dateparser
from bot.utils.datetime import parse_datetime_ru
from aiogram import Dispatcher, F
from aiogram.filters import StateFilter
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.fsm.context import FSMContext

from bot.deps import AppDeps
from bot.db import (
    db_add_event,
    db_log_error,
    get_current_project_id,
    fetch_portfolio_rows,
    ensure_inbox_project_id,
)
from bot.fsm import (
    AddTaskWizard,
    AddReminderWizard,
    AddPersonalWizard,
    AddEventWizard,
    QuickTaskWizard,
    QuickIdeaWizard,
    AddSuperTaskWizard,
)
from bot.handlers.common import escape_hatch_menu_or_command
from bot.keyboards import main_menu_kb, back_home_kb
from bot.services.background import fire_and_forget
from bot.services.gtasks_service import get_or_create_list_id, due_from_local_date
from bot.services.vault_sync import background_project_sync, background_log_event
from bot.ui.render import ui_safe_edit as safe_edit, ui_safe_wizard_render as wizard_render
from bot.ui.screens import ui_render_home, ui_render_projects_portfolio
from bot.ui.state import ui_get_state, ui_set_state
from bot.utils import (
    h,
    kb_columns,
    quick_extract_datetime_ru,
    quick_parse_datetime_ru,
    try_delete_user_message,
)





UTC = ZoneInfo("UTC")


def _tz_from_deps(deps: AppDeps) -> ZoneInfo:
    """Resolve app timezone.

    Always prefer explicit env vars (BOT_TIMEZONE/APP_TIMEZONE/BOT_TZ),
    then fall back to deps.tz_name.
    """
    name = resolve_tz_name((deps.tz_name or "Europe/Moscow"))
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("Europe/Moscow")


def to_deadline_db(dt_local: datetime, deps: AppDeps) -> datetime:
    """Convert local deadline to DB representation (UTC naive or UTC aware).

    - TIMESTAMP schema: store naive UTC
    - TIMESTAMPTZ schema: store aware UTC to avoid session-TZ casts
    """

    if dt_local.tzinfo is None:
        dt_local = dt_local.replace(tzinfo=_tz_from_deps(deps))
    return to_db_utc(
        dt_local,
        tz_name=deps.tz_name,
        store_tz=bool(getattr(deps, 'db_tasks_deadline_timestamptz', False)),
    )


def fmt_local(dt_utc_or_naive: datetime | None, deps: AppDeps) -> str:
    if dt_utc_or_naive is None:
        return "—"
    if dt_utc_or_naive.tzinfo is None:
        dt_utc_or_naive = dt_utc_or_naive.replace(tzinfo=UTC)
    return dt_utc_or_naive.astimezone(_tz_from_deps(deps)).strftime("%d.%m %H:%M")


async def _guard(callback: CallbackQuery, deps: AppDeps) -> bool:
    if deps.admin_id and callback.from_user and callback.from_user.id != deps.admin_id:
        await callback.answer("Недоступно", show_alert=True)
        return False
    return True


# ---------------------------------------------------------------------------
# Common
# ---------------------------------------------------------------------------


async def cb_add_cancel(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    await callback.answer()
    await state.clear()
    await ui_render_home(callback.message, db_pool, tz_name=deps.tz_name, force_new=False)


# ---------------------------------------------------------------------------
# Task wizard (incl. Subtask)
# ---------------------------------------------------------------------------


def _super_confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Создать", callback_data="add:super:create")],
            [InlineKeyboardButton(text="✖️ Отмена", callback_data="add:cancel")],
        ]
    )


async def cb_add_super_start(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if not await _guard(callback, deps):
        return
    await callback.answer()
    await state.clear()

    parts = (callback.data or "").split(":")
    project_id = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else None
    if not project_id:
        return

    await state.update_data(
        wizard_mode="super",
        project_id=int(project_id),
        wizard_chat_id=int(callback.message.chat.id),
        wizard_msg_id=int(callback.message.message_id),
    )
    await state.set_state(AddSuperTaskWizard.entering_title)
    return await safe_edit(
        callback.message,
        "🧩 <b>Суперзадача</b>\n\nВведите название (это контейнер для задач внутри проекта).",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data="add:cancel")]]),
        parse_mode="HTML",
    )


async def msg_add_super_title(message: Message, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if deps.admin_id and (not message.from_user or message.from_user.id != deps.admin_id):
        return
    title = (message.text or "").strip()
    if not title:
        return await wizard_render(
            bot=message.bot,
            state=state,
            chat_id=int(message.chat.id),
            fallback_msg=None,
            text="Введите название суперзадачи.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data="add:cancel")]]
            ),
        )

    data = await state.get_data()
    project_id = data.get("project_id")
    if not project_id:
        await state.clear()
        return await wizard_render(
            bot=message.bot,
            state=state,
            chat_id=int(message.chat.id),
            fallback_msg=None,
            text="❌ Не удалось определить проект.",
            reply_markup=back_home_kb(),
            parse_mode="HTML",
        )

    # Keep chat clean: the wizard already owns the SPA message, so we can delete user input.
    await try_delete_user_message(message)

    await state.update_data(title=title)
    await state.set_state(AddSuperTaskWizard.confirming)

    project_code = "—"
    try:
        async with db_pool.acquire() as conn:
            project_code = await conn.fetchval("SELECT code FROM projects WHERE id=$1", int(project_id)) or "—"
    except Exception:
        project_code = "—"

    lines = [
        "🧩 <b>Проверь суперзадачу</b>",
        f"Проект: <b>{h(project_code)}</b>",
        "",
        f"🧩 {h(title)}",
        "",
        "Создать суперзадачу?",
    ]
    return await wizard_render(
        bot=message.bot,
        state=state,
        chat_id=int(message.chat.id),
        fallback_msg=message,
        text="\n".join(lines),
        reply_markup=_super_confirm_kb(),
        parse_mode="HTML",
    )


async def cb_add_super_create(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if not await _guard(callback, deps):
        return
    await callback.answer()
    data = await state.get_data()
    project_id = data.get("project_id")
    title = (data.get("title") or "").strip()
    if not project_id or not title:
        await state.clear()
        return await ui_render_home(callback.message, db_pool, tz_name=deps.tz_name, force_new=False)

    vault = deps.vault
    task_id = None
    try:
        async with db_pool.acquire() as conn:
            proj = await conn.fetchrow("SELECT id, code FROM projects WHERE id=$1", int(project_id))
            if not proj:
                await state.clear()
                return await safe_edit(callback.message, "❌ Проект не найден.", reply_markup=back_home_kb(), parse_mode="HTML")

            task_id = await conn.fetchval(
                "INSERT INTO tasks (project_id, title, assignee_id, deadline, parent_task_id, kind) "
                "VALUES ($1,$2,NULL,NULL,NULL,'super') RETURNING id",
                int(project_id),
                str(title),
            )
            await db_add_event(
                conn,
                "super_task_created",
                int(project_id),
                int(task_id),
                f"🧩 Создана суперзадача | [{proj['code']}] #{int(task_id)} {title}",
            )
    except Exception as e:
        await state.clear()
        return await wizard_render(
            bot=callback.bot,
            state=state,
            chat_id=int(callback.message.chat.id),
            fallback_msg=callback.message,
            text=f"❌ Ошибка загрузки. Для фикса: {h(str(e))}",
            reply_markup=main_menu_kb(),
            parse_mode="HTML",
        )

    fire_and_forget(
        background_project_sync(int(project_id), db_pool, vault, error_logger=lambda w, e, c: db_log_error(db_pool, w, e, c)),
        label="vault_sync",
    )
    await state.clear()
    return await safe_edit(
        callback.message,
        f"✅ Суперзадача создана: <b>#{int(task_id)}</b> {h(title)}",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="⬅ Проект", callback_data=f"proj:{int(project_id)}")],
                [InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home")],
            ]
        ),
        parse_mode="HTML",
    )


def deadline_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Сегодня", callback_data="add:dl:today"),
                InlineKeyboardButton(text="Завтра", callback_data="add:dl:tomorrow"),
            ],
            [
                InlineKeyboardButton(text="+3 дня", callback_data="add:dl:+3"),
                InlineKeyboardButton(text="+7 дней", callback_data="add:dl:+7"),
            ],
            [
                InlineKeyboardButton(text="Без срока", callback_data="add:dl:none"),
                InlineKeyboardButton(text="Ввести дату", callback_data="add:dl:manual"),
            ],
            [InlineKeyboardButton(text="✖️ Отмена", callback_data="add:cancel")],
        ]
    )


def _task_confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Создать", callback_data="add:create")],
            [InlineKeyboardButton(text="✏️ Срок", callback_data="add:edit_deadline")],
            [InlineKeyboardButton(text="✖️ Отмена", callback_data="add:cancel")],
        ]
    )


async def _task_render_confirm(msg: Message, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    data = await state.get_data()
    title = (data.get("title") or "").strip()
    project_id = data.get("project_id")
    assignee_id = data.get("assignee_id")
    deadline_iso = data.get("deadline_msk")

    project_code = "—"
    assignee_name = "—"
    dl_txt = "без срока"

    try:
        if project_id:
            async with db_pool.acquire() as conn:
                project_code = await conn.fetchval("SELECT code FROM projects WHERE id=$1", int(project_id)) or "—"
                if assignee_id is None:
                    assignee_name = "—"
                else:
                    assignee_name = await conn.fetchval("SELECT name FROM team WHERE id=$1", int(assignee_id)) or "—"
    except Exception:
        pass

    if deadline_iso:
        try:
            dl_dt = datetime.fromisoformat(str(deadline_iso))
            dl_txt = fmt_local(dl_dt, deps)
        except Exception:
            dl_txt = "—"

    lines = [
        "📝 <b>Проверь задачу</b>",
        f"Проект: <b>{h(project_code)}</b>",
        f"Исполнитель: <b>{h(assignee_name)}</b>",
        f"Срок: <b>{h(dl_txt)}</b>",
        "",
        f"📝 {h(title)}",
        "",
        "Создать задачу?",
    ]
    await wizard_render(
        bot=msg.bot,
        state=state,
        chat_id=int(msg.chat.id),
        fallback_msg=msg,
        text="\n".join(lines),
        reply_markup=_task_confirm_kb(),
        parse_mode="HTML",
    )


async def cb_add_task_start(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if not await _guard(callback, deps):
        return
    await callback.answer()
    await state.clear()
    await state.update_data(
        wizard_mode="task",
        parent_task_id=None,
        wizard_chat_id=int(callback.message.chat.id),
        wizard_msg_id=int(callback.message.message_id),
    )

    parts = (callback.data or "").split(":")
    forced_project_id = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else None

    async with db_pool.acquire() as conn:
        current_id = await get_current_project_id(conn, int(callback.message.chat.id)) or None
        projects = await conn.fetch("SELECT id, code FROM projects WHERE status='active' ORDER BY created_at DESC")

    if not projects:
        await safe_edit(callback.message, "📭 Активных проектов нет — сначала создайте проект.")
        return

    if forced_project_id:
        await state.update_data(project_id=int(forced_project_id))
        await state.set_state(AddTaskWizard.choosing_assignee)
        return await show_assignee_picker(callback.message, db_pool, deps)

    kb_rows: list[list[InlineKeyboardButton]] = []
    if current_id:
        cur = next((p for p in projects if int(p["id"]) == int(current_id)), None)
        if cur:
            kb_rows.append(
                [InlineKeyboardButton(text=f"Текущий ({cur['code']})", callback_data=f"add:proj:{cur['id']}")]
            )
    kb_rows.append([InlineKeyboardButton(text="Выбрать проект", callback_data="add:proj:choose")])
    kb_rows.append([InlineKeyboardButton(text="✖️ Отмена", callback_data="add:cancel")])

    await state.set_state(AddTaskWizard.choosing_project)
    await safe_edit(
        callback.message,
        "➕ Добавить задачу: выберите проект",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
    )


async def cb_add_choose_project(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if not await _guard(callback, deps):
        return
    await callback.answer()

    async with db_pool.acquire() as conn:
        current_id = await get_current_project_id(conn, int(callback.message.chat.id)) or None
        rows = await fetch_portfolio_rows(conn)

    if not rows:
        await safe_edit(callback.message, "📭 Активных проектов нет.")
        return

    def sort_key(r):
        is_cur = (current_id is not None and int(r["id"]) == int(current_id))
        is_inbox = (r.get("code") == "INBOX")
        priority = 0 if is_cur else (1 if is_inbox else 2)
        return (priority, -int(r.get("overdue_tasks") or 0), -int(r.get("active_tasks") or 0), r["code"])

    rows_sorted = sorted(list(rows), key=sort_key)
    kb: list[list[InlineKeyboardButton]] = []
    row_btns: list[InlineKeyboardButton] = []
    for r in rows_sorted:
        label = r["code"]
        if current_id is not None and int(r["id"]) == int(current_id):
            label = f"⭐ {label}"
        elif int(r.get("overdue_tasks") or 0) > 0:
            label = f"🚨{r['overdue_tasks']} {label}"
        row_btns.append(InlineKeyboardButton(text=label, callback_data=f"add:proj:{r['id']}") )
        if len(row_btns) == 2:
            kb.append(row_btns)
            row_btns = []
    if row_btns:
        kb.append(row_btns)
    kb.append([InlineKeyboardButton(text="✖️ Отмена", callback_data="add:cancel")])
    await safe_edit(callback.message, "Выберите проект:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))


async def cb_add_set_project(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if not await _guard(callback, deps):
        return
    await callback.answer()
    parts = (callback.data or "").split(":")
    if len(parts) < 3 or not parts[2].isdigit():
        return
    project_id = int(parts[2])
    await state.update_data(project_id=project_id)

    await state.set_state(AddTaskWizard.choosing_assignee)
    return await show_assignee_picker(callback.message, db_pool, deps)


async def show_assignee_picker(msg: Message, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    async with db_pool.acquire() as conn:
        team = await conn.fetch("SELECT id, name FROM team ORDER BY name")

    kb: list[list[InlineKeyboardButton]] = [[InlineKeyboardButton(text="Без исполнителя", callback_data="add:as:none")]]
    buttons = [InlineKeyboardButton(text=str(r["name"]), callback_data=f"add:as:{r['id']}") for r in team]
    kb.extend(kb_columns(buttons, 2))
    kb.append([InlineKeyboardButton(text="✖️ Отмена", callback_data="add:cancel")])
    await safe_edit(msg, "Выберите исполнителя:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))


async def cb_add_set_assignee(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if not await _guard(callback, deps):
        return
    await callback.answer()
    token = (callback.data or "").split(":")[2]
    assignee_id = None if token == "none" else int(token)
    await state.update_data(assignee_id=assignee_id)

    data = await state.get_data()
    if data.get("title"):
        await state.set_state(AddTaskWizard.choosing_deadline)
        return await wizard_render(
            bot=callback.bot,
            state=state,
            chat_id=int(callback.message.chat.id),
            fallback_msg=callback.message,
            text="Выберите срок задачи или отправьте дату/время сообщением:",
            reply_markup=deadline_kb(),
        )

    await state.set_state(AddTaskWizard.entering_title)
    await wizard_render(
        bot=callback.bot,
        state=state,
        chat_id=int(callback.message.chat.id),
        fallback_msg=callback.message,
        text="Введите текст задачи одной строкой (сообщением).",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data="add:cancel")]]),
    )


async def msg_add_task_title(message: Message, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if deps.admin_id and (not message.from_user or message.from_user.id != deps.admin_id):
        return


    if await escape_hatch_menu_or_command(message, state, db_pool):
        return
    await try_delete_user_message(message)
    title_raw = (message.text or "").strip()
    if not title_raw:
        await wizard_render(
            bot=message.bot,
            state=state,
            chat_id=int(message.chat.id),
            fallback_msg=None,
            text="Текст пустой. Введите текст задачи.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data="add:cancel")]]),
        )
        return

    dt = quick_parse_datetime_ru(title_raw, deps.tz_name, date_only_time=(18, 0))
    await state.update_data(title=title_raw)

    if dt is not None:
        # Store local datetime in state for display and later conversion
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_tz_from_deps(deps))
        await state.update_data(deadline_msk=dt.astimezone(_tz_from_deps(deps)).isoformat())
        await state.set_state(AddTaskWizard.confirming)
        return await _task_render_confirm(message, state, db_pool, deps)

    await state.set_state(AddTaskWizard.choosing_deadline)
    await wizard_render(
        bot=message.bot,
        state=state,
        chat_id=int(message.chat.id),
        fallback_msg=None,
        text="Выберите срок задачи или отправьте дату/время сообщением:",
        reply_markup=deadline_kb(),
    )


async def cb_add_deadline(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if not await _guard(callback, deps):
        return
    await callback.answer()
    kind = (callback.data or "").split(":")[2]
    tz = _tz_from_deps(deps)
    now_local = datetime.now(tz)
    deadline_local: datetime | None = None

    if kind == "today":
        deadline_local = now_local.replace(hour=18, minute=0, second=0, microsecond=0)
    elif kind == "tomorrow":
        dt = now_local + timedelta(days=1)
        deadline_local = dt.replace(hour=18, minute=0, second=0, microsecond=0)
    elif kind == "+3":
        dt = now_local + timedelta(days=3)
        deadline_local = dt.replace(hour=18, minute=0, second=0, microsecond=0)
    elif kind == "+7":
        dt = now_local + timedelta(days=7)
        deadline_local = dt.replace(hour=18, minute=0, second=0, microsecond=0)
    elif kind == "none":
        deadline_local = None
    elif kind == "manual":
        await state.set_state(AddTaskWizard.entering_deadline)
        return await wizard_render(
            bot=callback.bot,
            state=state,
            chat_id=int(callback.message.chat.id),
            fallback_msg=callback.message,
            text="Введите дату/время (например 26.02 14:00 или 26.02).",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data="add:cancel")]]),
        )

    await state.update_data(deadline_msk=(deadline_local.isoformat() if deadline_local else None))
    await state.set_state(AddTaskWizard.confirming)
    return await _task_render_confirm(callback.message, state, db_pool, deps)


async def msg_add_task_deadline(message: Message, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if deps.admin_id and (not message.from_user or message.from_user.id != deps.admin_id):
        return


    if await escape_hatch_menu_or_command(message, state, db_pool):
        return
    await try_delete_user_message(message)
    raw = (message.text or "").strip()
    parsed = await asyncio.to_thread(parse_datetime_ru, raw, deps.tz_name, prefer_future=True)
    if not parsed:
        return await wizard_render(
            bot=message.bot,
            state=state,
            chat_id=int(message.chat.id),
            fallback_msg=None,
            text="Не понял дату. Пример: 26.02 14:00",
        )
    tz = _tz_from_deps(deps)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    dl_local = parsed.astimezone(tz)
    await state.update_data(deadline_msk=dl_local.isoformat())
    await state.set_state(AddTaskWizard.confirming)
    return await _task_render_confirm(message, state, db_pool, deps)


async def cb_add_edit_deadline(callback: CallbackQuery, state: FSMContext, deps: AppDeps) -> None:
    if not await _guard(callback, deps):
        return
    await callback.answer()
    await state.set_state(AddTaskWizard.choosing_deadline)
    await wizard_render(
        bot=callback.bot,
        state=state,
        chat_id=int(callback.message.chat.id),
        fallback_msg=callback.message,
        text="Выберите срок задачи или отправьте дату/время сообщением:",
        reply_markup=deadline_kb(),
        parse_mode="HTML",
    )


async def create_task_from_wizard(
    msg: Message,
    state: FSMContext,
    db_pool: asyncpg.Pool,
    deps: AppDeps,
    deadline_local: datetime | None,
) -> None:
    data = await state.get_data()
    vault = deps.vault
    project_id = data.get("project_id")
    title = data.get("title")
    parent_task_id = data.get("parent_task_id")
    wizard_mode = data.get("wizard_mode", "task")

    if project_id is None or not title or "assignee_id" not in data:
        await state.clear()
        return await wizard_render(
            bot=msg.bot,
            state=state,
            chat_id=int(msg.chat.id),
            fallback_msg=msg,
            text="⚠️ Не хватает данных для создания задачи. Начните заново через ➕ Добавить.",
            reply_markup=main_menu_kb(),
        )

    assignee_id = data.get("assignee_id")
    if wizard_mode == "subtask" and not parent_task_id:
        await state.clear()
        return await wizard_render(
            bot=msg.bot,
            state=state,
            chat_id=int(msg.chat.id),
            fallback_msg=msg,
            text="⚠️ Для подзадачи нужно выбрать родительскую задачу. Начните заново.",
            reply_markup=main_menu_kb(),
        )

    deadline_utc = to_deadline_db(deadline_local, deps) if deadline_local else None

    try:
        async with db_pool.acquire() as conn:
            proj = await conn.fetchrow("SELECT id, code, name FROM projects WHERE id=$1", int(project_id))
            tm_name = "—"
            if assignee_id is not None:
                tm_name = await conn.fetchval("SELECT name FROM team WHERE id=$1", int(assignee_id)) or "—"

            task_id = await conn.fetchval(
                "INSERT INTO tasks (project_id, title, assignee_id, deadline, parent_task_id) VALUES ($1,$2,$3,$4,$5) RETURNING id",
                int(project_id),
                str(title),
                assignee_id,
                deadline_utc,
                parent_task_id,
            )
            await db_add_event(conn, "task_created", int(project_id), int(task_id), f"🆕 Создано | [{proj['code']}] #{task_id} {title}")

    except Exception as e:
        await state.clear()
        return await wizard_render(
            bot=msg.bot,
            state=state,
            chat_id=int(msg.chat.id),
            fallback_msg=msg,
            text=f"❌ Ошибка загрузки. Для фикса: {h(str(e))}",
            reply_markup=main_menu_kb(),
            parse_mode="HTML",
        )

    # Daily log + project sync
    fire_and_forget(
        background_log_event(
            f"Создана задача [{proj['code']}] для {tm_name}: {title}",
            vault,
            error_logger=lambda w, e, c: db_log_error(db_pool, w, e, c),
        ),
        label="log",
    )
    fire_and_forget(
        background_project_sync(int(project_id), db_pool, vault, error_logger=lambda w, e, c: db_log_error(db_pool, w, e, c)),
        label="vault_sync",
    )

    await state.clear()
    await safe_edit(
        msg,
        f"✅ Задача создана: <b>{h(proj['code'])}</b> | <b>{h(tm_name)}</b>\n<i>[ID:{task_id}]</i> {h(title)}",
        reply_markup=back_home_kb(),
        parse_mode="HTML",
    )


async def cb_add_create_task(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if not await _guard(callback, deps):
        return
    await callback.answer()
    data = await state.get_data()
    dl_iso = data.get("deadline_msk")
    deadline_local = None
    if dl_iso:
        try:
            deadline_local = datetime.fromisoformat(str(dl_iso))
        except Exception:
            deadline_local = None
    return await create_task_from_wizard(callback.message, state, db_pool, deps, deadline_local)


async def cb_add_subtask(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if not await _guard(callback, deps):
        return
    await callback.answer()
    try:
        parent_id = int((callback.data or "").split(":")[2])
    except Exception:
        return

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT project_id, kind FROM tasks WHERE id=$1", parent_id)
    if not row:
        return await safe_edit(callback.message, "❌ Родительская задача не найдена.")
    if (row.get("kind") or "task") != "super":
        await callback.answer("Подзадачи доступны только в суперзадачах", show_alert=True)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="⬅ Назад", callback_data=f"task:{int(parent_id)}")],
                [InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home")],
            ]
        )
        return await safe_edit(
            callback.message,
            "🧩 Подзадачи доступны только в <b>суперзадачах</b>.",
            reply_markup=kb,
            parse_mode="HTML",
        )

    await state.clear()
    await state.update_data(
        wizard_mode="subtask",
        project_id=int(row["project_id"]),
        parent_task_id=parent_id,
        wizard_chat_id=int(callback.message.chat.id),
        wizard_msg_id=int(callback.message.message_id),
    )
    await state.set_state(AddTaskWizard.choosing_assignee)
    return await show_assignee_picker(callback.message, db_pool, deps)


# ---------------------------------------------------------------------------
# Reminder wizard
# ---------------------------------------------------------------------------


def reminder_time_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="+15 мин", callback_data="add:rtime:+15m"),
                InlineKeyboardButton(text="+1 час", callback_data="add:rtime:+1h"),
            ],
            [
                InlineKeyboardButton(text="Сегодня 18:00", callback_data="add:rtime:today18"),
                InlineKeyboardButton(text="Завтра 09:00", callback_data="add:rtime:tomorrow9"),
            ],
            [
                InlineKeyboardButton(text="Ввести дату", callback_data="add:rtime:manual"),
                InlineKeyboardButton(text="✖️ Отмена", callback_data="add:cancel"),
            ],
        ]
    )


def reminder_repeat_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Нет", callback_data="add:rrep:none"),
                InlineKeyboardButton(text="Каждый день", callback_data="add:rrep:daily"),
            ],
            [
                InlineKeyboardButton(text="По будням", callback_data="add:rrep:workdays"),
                InlineKeyboardButton(text="Каждую неделю", callback_data="add:rrep:weekly"),
            ],
            [
                InlineKeyboardButton(text="Каждый месяц", callback_data="add:rrep:monthly"),
                InlineKeyboardButton(text="✖️ Отмена", callback_data="add:cancel"),
            ],
        ]
    )


async def cb_add_reminder_start(callback: CallbackQuery, state: FSMContext, deps: AppDeps) -> None:
    if not await _guard(callback, deps):
        return
    await callback.answer()
    await state.clear()
    await state.update_data(wizard_chat_id=int(callback.message.chat.id), wizard_msg_id=int(callback.message.message_id))
    await state.set_state(AddReminderWizard.choosing_time)
    await wizard_render(
        bot=callback.bot,
        state=state,
        chat_id=int(callback.message.chat.id),
        fallback_msg=callback.message,
        text="⏰ <b>Добавить напоминание</b>: выберите время или отправьте дату/время сообщением",
        reply_markup=reminder_time_kb(),
        parse_mode="HTML",
    )


async def cb_add_reminder_time(callback: CallbackQuery, state: FSMContext, deps: AppDeps) -> None:
    if not await _guard(callback, deps):
        return
    await callback.answer()
    kind = (callback.data or "").split(":")[2]
    tz = _tz_from_deps(deps)
    now_local = datetime.now(tz)

    if kind == "manual":
        await state.set_state(AddReminderWizard.entering_time)
        return await wizard_render(
            bot=callback.bot,
            state=state,
            chat_id=int(callback.message.chat.id),
            fallback_msg=callback.message,
            text="Введите дату/время (например 26.02 14:00 или завтра 9:00).",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data="add:cancel")]]),
        )

    remind_local: datetime | None = None
    if kind == "+15m":
        remind_local = now_local + timedelta(minutes=15)
    elif kind == "+1h":
        remind_local = now_local + timedelta(hours=1)
    elif kind == "today18":
        remind_local = now_local.replace(hour=18, minute=0, second=0, microsecond=0)
        if remind_local <= now_local:
            remind_local = now_local + timedelta(minutes=5)
    elif kind == "tomorrow9":
        remind_local = (now_local + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)

    if not remind_local:
        await state.clear()
        return await wizard_render(
            bot=callback.bot,
            state=state,
            chat_id=int(callback.message.chat.id),
            fallback_msg=callback.message,
            text="⚠️ Не удалось распознать время. Попробуйте снова.",
            reply_markup=reminder_time_kb(),
        )

    remind_utc = remind_local.astimezone(UTC)
    await state.update_data(remind_at=remind_utc)
    await state.set_state(AddReminderWizard.entering_text)
    return await wizard_render(
        bot=callback.bot,
        state=state,
        chat_id=int(callback.message.chat.id),
        fallback_msg=callback.message,
        text=f"Ок. Напомню <b>{h(remind_local.strftime('%d.%m %H:%M'))}</b>.\nВведите текст напоминания одной строкой.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data="add:cancel")]]),
        parse_mode="HTML",
    )


async def msg_add_reminder_time(message: Message, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if deps.admin_id and (not message.from_user or message.from_user.id != deps.admin_id):
        return
    if await escape_hatch_menu_or_command(message, state, db_pool):
        return
    await try_delete_user_message(message)
    parsed = await asyncio.to_thread(parse_datetime_ru, message.text or "", deps.tz_name, prefer_future=True)
    if not parsed:
        return await wizard_render(
            bot=message.bot,
            state=state,
            chat_id=int(message.chat.id),
            fallback_msg=None,
            text="Не понял дату. Пример: 26.02 14:00 или завтра 9:00.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data="add:cancel")]]),
        )
    tz = _tz_from_deps(deps)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    remind_utc = parsed.astimezone(UTC)
    if remind_utc <= datetime.now(UTC):
        return await wizard_render(
            bot=message.bot,
            state=state,
            chat_id=int(message.chat.id),
            fallback_msg=None,
            text="Время уже прошло. Укажите время в будущем.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data="add:cancel")]]),
        )
    await state.update_data(remind_at=remind_utc)
    await state.set_state(AddReminderWizard.entering_text)
    tz = _tz_from_deps(deps)
    return await wizard_render(
        bot=message.bot,
        state=state,
        chat_id=int(message.chat.id),
        fallback_msg=None,
        text=f"Ок. Напомню <b>{h(remind_utc.astimezone(tz).strftime('%d.%m %H:%M'))}</b>.\nВведите текст напоминания одной строкой.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data="add:cancel")]]),
        parse_mode="HTML",
    )


async def msg_add_reminder_text(message: Message, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if deps.admin_id and (not message.from_user or message.from_user.id != deps.admin_id):
        return
    if await escape_hatch_menu_or_command(message, state, db_pool):
        return
    await try_delete_user_message(message)
    data = await state.get_data()
    remind_at = data.get("remind_at")
    text_part = (message.text or "").strip()
    if not text_part:
        return await wizard_render(
            bot=message.bot,
            state=state,
            chat_id=int(message.chat.id),
            fallback_msg=None,
            text="Текст пустой. Введите текст напоминания.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data="add:cancel")]]),
        )
    if not remind_at:
        await wizard_render(
            bot=message.bot,
            state=state,
            chat_id=int(message.chat.id),
            fallback_msg=None,
            text="⚠️ Не выбрано время. Начните заново через ➕ Добавить.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home")]]),
        )
        await state.clear()
        return

    await state.update_data(text=text_part)
    await state.set_state(AddReminderWizard.choosing_repeat)
    return await wizard_render(
        bot=message.bot,
        state=state,
        chat_id=int(message.chat.id),
        fallback_msg=None,
        text="Повторять это напоминание?",
        reply_markup=reminder_repeat_kb(),
    )



async def cb_add_reminder_repeat(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if not await _guard(callback, deps):
        return
    await callback.answer()
    repeat = (callback.data or "").split(":")[2]
    if repeat not in {"none", "daily", "weekly", "workdays", "monthly"}:
        repeat = "none"

    data = await state.get_data()
    remind_at = data.get("remind_at")
    text_part = (data.get("text") or "").strip()
    if not remind_at or not text_part:
        await wizard_render(
            bot=callback.bot,
            state=state,
            chat_id=int(callback.message.chat.id),
            fallback_msg=callback.message,
            text="⚠️ Данные напоминания потеряны. Начните заново через ➕ Добавить.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home")]]),
        )
        await state.clear()
        return

    try:
        remind_at_dt = remind_at
        if isinstance(remind_at_dt, str):
            remind_at_dt = datetime.fromisoformat(remind_at_dt)
        if remind_at_dt.tzinfo is None:
            remind_at_dt = remind_at_dt.replace(tzinfo=UTC)

        remind_at_db = to_db_utc(
            remind_at_dt,
            tz_name=deps.tz_name,
            store_tz=bool(getattr(deps, 'db_reminders_remind_at_timestamptz', False)),
        )

        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO reminders (
                    chat_id,
                    text,
                    remind_at,
                    repeat,
                    status,
                    next_attempt_at_utc,
                    is_sent
                )
                VALUES ($1, $2, $3, $4, 'pending', $5, FALSE)
                """,
                int(callback.message.chat.id),
                text_part,
                remind_at_db,
                repeat,
                remind_at_dt.astimezone(UTC),
            )
            await db_add_event(conn, "reminder_created", None, None, f"Создано напоминание ({repeat}): {text_part}")
    except Exception as e:
        await wizard_render(
            bot=callback.bot,
            state=state,
            chat_id=int(callback.message.chat.id),
            fallback_msg=callback.message,
            text=f"❌ Ошибка загрузки. Для фикса: {h(str(e))}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home")]]),
            parse_mode="HTML",
        )
        await state.clear()
        return

    rep_txt = {
        "none": "без повтора",
        "daily": "каждый день",
        "weekly": "каждую неделю",
        "workdays": "по будням",
        "monthly": "каждый месяц",
    }.get(repeat, "без повтора")

    await wizard_render(
        bot=callback.bot,
        state=state,
        chat_id=int(callback.message.chat.id),
        fallback_msg=callback.message,
        text=f"✅ Напоминание создано (<i>{h(rep_txt)}</i>)\n\n<b>{h(text_part)}</b>\n<i>{h(fmt_local(remind_at_dt, deps))}</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home")]]),
        parse_mode="HTML",
    )
    await state.clear()


# ---------------------------------------------------------------------------
# Reminder -> Task conversion (keep SPA anchor)
# ---------------------------------------------------------------------------


async def cb_rem_task(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if not await _guard(callback, deps):
        return
    try:
        rem_id = int((callback.data or "").split(":")[2])
    except Exception:
        return await callback.answer("Ошибка", show_alert=True)

    async with db_pool.acquire() as conn:
        text = await conn.fetchval("SELECT text FROM reminders WHERE id=$1", rem_id)
    if not text:
        return await callback.answer("Напоминание не найдено", show_alert=True)

    await callback.answer()
    await state.clear()
    await try_delete_user_message(callback.message)

    chat_id = int(callback.message.chat.id)
    async with db_pool.acquire() as conn:
        ui_state = await ui_get_state(conn, chat_id)
        ui_msg_id = ui_state.get("ui_message_id")
        current_id = await get_current_project_id(conn, chat_id) or None
        rows = await fetch_portfolio_rows(conn)

    await state.update_data(
        wizard_mode="task",
        parent_task_id=None,
        wizard_chat_id=chat_id,
        wizard_msg_id=ui_msg_id,
        title=text,
    )
    await state.set_state(AddTaskWizard.choosing_project)

    if not rows:
        await wizard_render(
            bot=callback.bot,
            state=state,
            chat_id=chat_id,
            fallback_msg=None,
            text="📭 Активных проектов нет.",
            reply_markup=back_home_kb(),
            parse_mode="HTML",
        )
        await state.clear()
        return

    def sort_key(r):
        is_cur = (current_id is not None and int(r["id"]) == int(current_id))
        is_inbox = (r.get("code") == "INBOX")
        priority = 0 if is_cur else (1 if is_inbox else 2)
        return (priority, -int(r.get("overdue_tasks") or 0), -int(r.get("active_tasks") or 0), r["code"])

    rows_sorted = sorted(list(rows), key=sort_key)
    kb: list[list[InlineKeyboardButton]] = []
    row_btns: list[InlineKeyboardButton] = []
    for r in rows_sorted:
        label = r["code"]
        if current_id is not None and int(r["id"]) == int(current_id):
            label = f"⭐ {label}"
        elif int(r.get("overdue_tasks") or 0) > 0:
            label = f"🚨{r['overdue_tasks']} {label}"
        row_btns.append(InlineKeyboardButton(text=label, callback_data=f"add:proj:{r['id']}") )
        if len(row_btns) == 2:
            kb.append(row_btns)
            row_btns = []
    if row_btns:
        kb.append(row_btns)
    kb.append([InlineKeyboardButton(text="✖️ Отмена", callback_data="add:cancel")])

    await wizard_render(
        bot=callback.bot,
        state=state,
        chat_id=chat_id,
        fallback_msg=None,
        text=f"Создание задачи из напоминания:\n<b>{h(text)}</b>\n\nВыберите проект:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
        parse_mode="HTML",
    )

    # Persist wizard anchor as SPA UI message so nav:home edits the same screen
    try:
        data = await state.get_data()
        wiz_msg_id = data.get("wizard_msg_id")
        if wiz_msg_id:
            async with db_pool.acquire() as conn:
                await ui_set_state(conn, chat_id, ui_message_id=int(wiz_msg_id), ui_screen="add")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Personal (Google Tasks)
# ---------------------------------------------------------------------------


def personal_deadline_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Сегодня", callback_data="pers:dl:today"),
                InlineKeyboardButton(text="Завтра", callback_data="pers:dl:tomorrow"),
            ],
            [
                InlineKeyboardButton(text="+3 дня", callback_data="pers:dl:+3"),
                InlineKeyboardButton(text="+7 дней", callback_data="pers:dl:+7"),
            ],
            [
                InlineKeyboardButton(text="Без срока", callback_data="pers:dl:none"),
                InlineKeyboardButton(text="Ввести дату", callback_data="pers:dl:manual"),
            ],
            [InlineKeyboardButton(text="✖️ Отмена", callback_data="add:cancel")],
        ]
    )


async def cb_add_personal_start(callback: CallbackQuery, state: FSMContext, deps: AppDeps) -> None:
    if not await _guard(callback, deps):
        return
    await callback.answer()
    gtasks = deps.gtasks
    if not gtasks.enabled():
        return await safe_edit(
            callback.message,
            "❌ Google Tasks не настроен. Добавьте GOOGLE_CLIENT_ID/SECRET/REFRESH_TOKEN в ENV.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="⬅ Назад", callback_data="add:cancel")]]
            ),
        )
    await state.clear()
    await state.update_data(wizard_chat_id=int(callback.message.chat.id), wizard_msg_id=int(callback.message.message_id))
    await state.set_state(AddPersonalWizard.entering_text)
    await wizard_render(
        bot=callback.bot,
        state=state,
        chat_id=int(callback.message.chat.id),
        fallback_msg=callback.message,
        text="🏡 <b>Личное</b> (в Google Tasks): отправьте текст задачи одним сообщением.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data="add:cancel")]]),
        parse_mode="HTML",
    )


async def msg_personal_text(message: Message, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if deps.admin_id and (not message.from_user or message.from_user.id != deps.admin_id):
        return
    if await escape_hatch_menu_or_command(message, state, db_pool):
        return
    await try_delete_user_message(message)
    text = (message.text or "").strip()
    if not text:
        return await wizard_render(
            bot=message.bot,
            state=state,
            chat_id=int(message.chat.id),
            fallback_msg=None,
            text="Текст пустой. Пришлите текст личной задачи.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data="add:cancel")]]),
        )
    await state.update_data(personal_text=text)
    await state.set_state(AddPersonalWizard.choosing_deadline)
    await wizard_render(
        bot=message.bot,
        state=state,
        chat_id=int(message.chat.id),
        fallback_msg=None,
        text="Выберите срок (необязательно) или отправьте дату/время сообщением:",
        reply_markup=personal_deadline_kb(),
    )


async def _create_personal_in_gtasks(
    msg: Message,
    state: FSMContext,
    db_pool: asyncpg.Pool,
    deps: AppDeps,
    due_local: datetime | None,
) -> None:
    data = await state.get_data()
    gtasks = deps.gtasks
    text = (data.get("personal_text") or "").strip()
    if not text:
        await wizard_render(
            bot=msg.bot,
            state=state,
            chat_id=int(msg.chat.id),
            fallback_msg=msg,
            text="⚠️ Данные потеряны. Начните заново через ➕ Добавить.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home")]]),
        )
        await state.clear()
        return

    list_name = os.getenv("GTASKS_PERSONAL_LIST", "Личное")
    tz = _tz_from_deps(deps)

    try:
        list_id = await get_or_create_list_id(db_pool, gtasks, list_name)
        due_utc = due_from_local_date(due_local, tz)
        await gtasks.create_task(list_id, text, due=due_utc)
        dl_txt = "без срока" if due_local is None else due_local.astimezone(tz).strftime("%d.%m")
        await wizard_render(
            bot=msg.bot,
            state=state,
            chat_id=int(msg.chat.id),
            fallback_msg=msg,
            text=f"✅ Добавлено в Google Tasks («{h(list_name)}»):\n\n<b>{h(text)}</b>\n<i>Срок: {h(dl_txt)}</i>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home")]]),
            parse_mode="HTML",
        )
    except Exception as e:
        await wizard_render(
            bot=msg.bot,
            state=state,
            chat_id=int(msg.chat.id),
            fallback_msg=msg,
            text=f"❌ Ошибка Google Tasks: {h(str(e))}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home")]]),
            parse_mode="HTML",
        )
    finally:
        await state.clear()


async def cb_personal_deadline(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if not await _guard(callback, deps):
        return
    await callback.answer()
    kind = (callback.data or "").split(":")[2]
    tz = _tz_from_deps(deps)
    now_local = datetime.now(tz)
    due_local: datetime | None = None

    if kind == "today":
        due_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    elif kind == "tomorrow":
        d = now_local + timedelta(days=1)
        due_local = d.replace(hour=0, minute=0, second=0, microsecond=0)
    elif kind == "+3":
        d = now_local + timedelta(days=3)
        due_local = d.replace(hour=0, minute=0, second=0, microsecond=0)
    elif kind == "+7":
        d = now_local + timedelta(days=7)
        due_local = d.replace(hour=0, minute=0, second=0, microsecond=0)
    elif kind == "none":
        due_local = None
    elif kind == "manual":
        await state.set_state(AddPersonalWizard.entering_deadline)
        return await wizard_render(
            bot=callback.bot,
            state=state,
            chat_id=int(callback.message.chat.id),
            fallback_msg=callback.message,
            text="Введите дату/время (например 26.02 или 26.02 14:00).",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data="add:cancel")]]),
        )

    return await _create_personal_in_gtasks(callback.message, state, db_pool, deps, due_local)


async def msg_personal_deadline(message: Message, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if deps.admin_id and (not message.from_user or message.from_user.id != deps.admin_id):
        return
    if await escape_hatch_menu_or_command(message, state, db_pool):
        return
    await try_delete_user_message(message)
    raw = (message.text or "").strip()
    parsed = await asyncio.to_thread(parse_datetime_ru, raw, deps.tz_name, prefer_future=True)
    if not parsed:
        return await wizard_render(
            bot=message.bot,
            state=state,
            chat_id=int(message.chat.id),
            fallback_msg=None,
            text="Не понял дату. Пример: 26.02 или 26.02 14:00",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data="add:cancel")]]),
        )
    tz = _tz_from_deps(deps)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    due_local = parsed.astimezone(tz)
    return await _create_personal_in_gtasks(message, state, db_pool, deps, due_local)


# ---------------------------------------------------------------------------
# Quick capture
# ---------------------------------------------------------------------------


def _quick_cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data="quick:cancel")]])


async def cb_quick_task(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if not await _guard(callback, deps):
        return
    await callback.answer()
    await state.clear()
    await state.set_state(QuickTaskWizard.entering_text)
    await wizard_render(
        bot=callback.bot,
        state=state,
        chat_id=int(callback.message.chat.id),
        fallback_msg=callback.message,
        text="⚡️ <b>Быстрая задача</b>\nОтправьте текст задачи. Умный парсер поймёт дату из текста.",
        reply_markup=_quick_cancel_kb(),
        parse_mode="HTML",
    )

    # Bind wizard message as current SPA UI anchor (important for reply-keyboard navigation)
    try:
        data = await state.get_data()
        wiz_msg_id = data.get("wizard_msg_id")
        if wiz_msg_id:
            async with db_pool.acquire() as conn:
                await ui_set_state(conn, int(callback.message.chat.id), ui_message_id=int(wiz_msg_id))
    except Exception:
        pass


async def cb_quick_idea(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if not await _guard(callback, deps):
        return
    await callback.answer()
    gtasks = deps.gtasks
    if not gtasks.enabled():
        return await safe_edit(callback.message, "❌ Google Tasks не настроен.", reply_markup=back_home_kb())
    await state.clear()
    await state.set_state(QuickIdeaWizard.entering_text)
    ideas_list = os.getenv("GTASKS_IDEAS_LIST", "Идеи")
    await wizard_render(
        bot=callback.bot,
        state=state,
        chat_id=int(callback.message.chat.id),
        fallback_msg=callback.message,
        text=f"💡 <b>Идея</b> (Google Tasks → «{h(ideas_list)}»)\nОтправьте текст идеи. Без сроков.",
        reply_markup=_quick_cancel_kb(),
        parse_mode="HTML",
    )

    # Bind wizard message as current SPA UI anchor (important for reply-keyboard navigation)
    try:
        data = await state.get_data()
        wiz_msg_id = data.get("wizard_msg_id")
        if wiz_msg_id:
            async with db_pool.acquire() as conn:
                await ui_set_state(conn, int(callback.message.chat.id), ui_message_id=int(wiz_msg_id))
    except Exception:
        pass


async def cb_quick_cancel(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if not await _guard(callback, deps):
        return
    await callback.answer()
    await state.clear()
    await ui_render_home(callback.message, db_pool, tz_name=deps.tz_name, force_new=False)


async def msg_quick_task_text(message: Message, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if deps.admin_id and (not message.from_user or message.from_user.id != deps.admin_id):
        return
    vault = deps.vault
    
    if await escape_hatch_menu_or_command(message, state, db_pool):
        return
    await try_delete_user_message(message)
    raw = (message.text or "").strip()
    if not raw:
        return

    tz = _tz_from_deps(deps)
    title, dt = quick_extract_datetime_ru(raw, deps.tz_name, date_only_time=(18, 0))
    deadline_local = dt.astimezone(tz) if dt and dt.tzinfo else (dt.replace(tzinfo=tz) if dt else None)
    title = (title or "").strip() or raw

    async with db_pool.acquire() as conn:
        inbox_id = await ensure_inbox_project_id(conn)
        task_id = await conn.fetchval(
            "INSERT INTO tasks (project_id, title, assignee_id, deadline) VALUES ($1,$2,NULL,$3) RETURNING id",
            inbox_id,
            title,
            to_deadline_db(deadline_local, deps) if deadline_local else None,
        )
        await db_add_event(conn, "task_created", inbox_id, int(task_id), f"🆕 INBOX #{task_id} {title}")

    fire_and_forget(
        background_project_sync(int(inbox_id), db_pool, vault, error_logger=lambda w, e, c: db_log_error(db_pool, w, e, c)),
        label="vault_sync",
    )

    # IMPORTANT: render into the existing wizard "screen" message.
    # If we clear FSM state before rendering, wizard_render won't know which
    # message to edit and will send a new message, leaving the wizard prompt
    # hanging in chat.
    await wizard_render(
        bot=message.bot,
        state=state,
        chat_id=int(message.chat.id),
        fallback_msg=None,
        text=f"✅ Добавлено во <b>Входящие</b>:\n{h(title)}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home")]]),
        parse_mode="HTML",
    )
    await state.clear()


async def msg_quick_idea_text(message: Message, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if deps.admin_id and (not message.from_user or message.from_user.id != deps.admin_id):
        return
    gtasks = deps.gtasks
    
    if await escape_hatch_menu_or_command(message, state, db_pool):
        return
    await try_delete_user_message(message)
    raw = (message.text or "").strip()
    if not raw:
        return

    ideas_list = os.getenv("GTASKS_IDEAS_LIST", "Идеи")
    try:
        list_id = await get_or_create_list_id(db_pool, gtasks, ideas_list)
        await gtasks.create_task(list_id, raw)
        await wizard_render(
            bot=message.bot,
            state=state,
            chat_id=int(message.chat.id),
            fallback_msg=None,
            text=f"✅ Добавлено в «{h(ideas_list)}»: <b>{h(raw)}</b>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home")]]),
            parse_mode="HTML",
        )
    except Exception as e:
        await wizard_render(
            bot=message.bot,
            state=state,
            chat_id=int(message.chat.id),
            fallback_msg=None,
            text=f"❌ Ошибка Google Tasks: {h(str(e))}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home")]]),
            parse_mode="HTML",
        )
    finally:
        await state.clear()


 # iCloud event wizard moved to bot.handlers.events


def register(dp: Dispatcher) -> None:
    # common
    dp.callback_query.register(cb_add_cancel, F.data == "add:cancel")

    # tasks
    dp.callback_query.register(cb_add_task_start, F.data.startswith("add:task"))
    dp.callback_query.register(cb_add_super_start, F.data.regexp(r"^add:super:\d+$"))
    dp.callback_query.register(cb_add_choose_project, F.data == "add:proj:choose")
    dp.callback_query.register(cb_add_set_project, F.data.regexp(r"^add:proj:\d+$"))
    dp.callback_query.register(cb_add_set_assignee, F.data.startswith("add:as:"))
    dp.message.register(msg_add_task_title, StateFilter(AddTaskWizard.entering_title), F.text)
    dp.callback_query.register(cb_add_deadline, StateFilter(AddTaskWizard.choosing_deadline), F.data.startswith("add:dl:"))
    dp.message.register(msg_add_task_deadline, StateFilter(AddTaskWizard.choosing_deadline), F.text)
    dp.message.register(msg_add_task_deadline, StateFilter(AddTaskWizard.entering_deadline), F.text)
    dp.callback_query.register(cb_add_edit_deadline, StateFilter(AddTaskWizard.confirming), F.data == "add:edit_deadline")
    dp.callback_query.register(cb_add_create_task, StateFilter(AddTaskWizard.confirming), F.data == "add:create")
    dp.callback_query.register(cb_add_subtask, F.data.startswith("add:sub:"))
    dp.message.register(msg_add_super_title, StateFilter(AddSuperTaskWizard.entering_title), F.text)
    dp.callback_query.register(cb_add_super_create, StateFilter(AddSuperTaskWizard.confirming), F.data == "add:super:create")

    # reminders
    dp.callback_query.register(cb_add_reminder_start, F.data == "add:rem")
    dp.callback_query.register(cb_add_reminder_time, StateFilter(AddReminderWizard.choosing_time), F.data.startswith("add:rtime:"))
    dp.message.register(msg_add_reminder_time, StateFilter(AddReminderWizard.choosing_time), F.text)
    dp.message.register(msg_add_reminder_time, StateFilter(AddReminderWizard.entering_time), F.text)
    dp.message.register(msg_add_reminder_text, StateFilter(AddReminderWizard.entering_text), F.text)
    dp.callback_query.register(cb_add_reminder_repeat, StateFilter(AddReminderWizard.choosing_repeat), F.data.startswith("add:rrep:"))
    dp.callback_query.register(cb_rem_task, F.data.startswith("rem:task:"))

    # personal
    dp.callback_query.register(cb_add_personal_start, F.data == "add:pers")
    dp.message.register(msg_personal_text, StateFilter(AddPersonalWizard.entering_text), F.text)
    dp.callback_query.register(cb_personal_deadline, StateFilter(AddPersonalWizard.choosing_deadline), F.data.startswith("pers:dl:"))
    dp.message.register(msg_personal_deadline, StateFilter(AddPersonalWizard.choosing_deadline), F.text)
    dp.message.register(msg_personal_deadline, StateFilter(AddPersonalWizard.entering_deadline), F.text)

    # quick capture
    dp.callback_query.register(cb_quick_task, F.data == "quick:task")
    dp.callback_query.register(cb_quick_idea, F.data == "quick:idea")
    dp.callback_query.register(cb_quick_cancel, F.data == "quick:cancel")
    dp.message.register(msg_quick_task_text, StateFilter(QuickTaskWizard.entering_text), F.text)
    dp.message.register(msg_quick_idea_text, StateFilter(QuickIdeaWizard.entering_text), F.text)

    # event wizard is registered in bot.handlers.events
