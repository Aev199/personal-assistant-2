"""iCloud (CalDAV) event creation wizard handlers."""

from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from bot.tz import resolve_tz_name

import asyncpg
import dateparser
from aiogram import Dispatcher, F
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.deps import AppDeps
from bot.db import db_add_event, get_current_project_id, db_log_error
from bot.fsm import AddEventWizard
from bot.handlers.common import escape_hatch_menu_or_command
from bot.keyboards.event import (
    event_kind_kb,
    event_title_kb,
    event_date_kb,
    event_time_kb,
    event_duration_kb,
    event_confirm_kb,
)
from bot.services.background import fire_and_forget
from bot.services.vault_sync import background_project_sync
from bot.ui.render import ui_safe_edit as safe_edit, ui_safe_wizard_render as wizard_render
from bot.ui.screens import ui_render_home
from bot.utils import h, quick_parse_datetime_ru, quick_parse_duration_min, try_delete_user_message


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





def _icloud_ready() -> tuple[bool, str]:
    apple_id = os.getenv("ICLOUD_APPLE_ID", "")
    app_pass = os.getenv("ICLOUD_APP_PASSWORD", "")
    url_work = os.getenv("ICLOUD_CALENDAR_URL_WORK", "")
    url_pers = os.getenv("ICLOUD_CALENDAR_URL_PERSONAL", "")

    if not (apple_id and app_pass):
        return False, "Не настроен доступ к iCloud CalDAV (ICLOUD_APPLE_ID / ICLOUD_APP_PASSWORD)."
    if not (url_work or url_pers):
        return False, "Не заданы URL календарей iCloud (нужен хотя бы один из ICLOUD_CALENDAR_URL_WORK / ICLOUD_CALENDAR_URL_PERSONAL)."
    return True, ""


async def _guard(callback: CallbackQuery, deps: AppDeps) -> bool:
    if deps.admin_id and callback.from_user and callback.from_user.id != deps.admin_id:
        await callback.answer("Недоступно", show_alert=True)
        return False
    return True


def _fmt_local(dt: datetime | None, tz: ZoneInfo) -> str:
    if not dt:
        return "—"
    if dt.tzinfo is None:
        # Treat naive as UTC.
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(tz).strftime("%d.%m %H:%M")


async def _event_render_confirm(msg: Message, state: FSMContext, deps: AppDeps) -> None:
    data = await state.get_data()

    tz = _tz_from_deps(deps)

    kind = data.get("kind") or "work"
    title = (data.get("title") or "").strip()
    project_code = data.get("project_code")
    include_project = bool(data.get("include_project", False))
    d_iso = data.get("date")
    t_hm = data.get("time")
    duration_min = data.get("duration_min")

    kind_txt = "Работа" if kind == "work" else "Личное"

    when_txt = "—"
    if d_iso and t_hm:
        try:
            d = datetime.fromisoformat(d_iso).date()
            hh, mm = map(int, t_hm.split(":", 1))
            dt_local = datetime(d.year, d.month, d.day, hh, mm, tzinfo=tz)
            when_txt = _fmt_local(dt_local, tz)
        except Exception:
            when_txt = "—"

    dur_txt = "—"
    try:
        if duration_min is not None:
            dur_txt = f"{int(duration_min)} мин"
    except Exception:
        dur_txt = "—"

    proj_txt = "—"
    if kind == "work" and project_code:
        proj_txt = str(project_code)

    preview_title = title
    if kind == "work" and include_project and project_code:
        code = str(project_code)
        if not preview_title.startswith(f"{code}:"):
            preview_title = f"{code}: {preview_title}"

    lines = [
        "📅 <b>Проверь событие</b>",
        f"Тип: <b>{h(kind_txt)}</b>",
        f"Дата/время: <b>{h(when_txt)}</b>",
        f"Длительность: <b>{h(dur_txt)}</b>",
        f"Проект: <b>{h(proj_txt)}</b>",
        "",
        f"📝 {h(preview_title)}",
        "",
        "Создать событие?",
    ]

    await wizard_render(
        bot=msg.bot,
        state=state,
        chat_id=int(msg.chat.id),
        fallback_msg=msg,
        text="\n".join(lines),
        reply_markup=event_confirm_kb(),
        parse_mode="HTML",
    )


async def cb_add_event_start(callback: CallbackQuery, state: FSMContext, deps: AppDeps) -> None:
    if not await _guard(callback, deps):
        return
    await callback.answer()

    ok, msg = _icloud_ready()
    if not ok:
        return await safe_edit(
            callback.message,
            f"❌ iCloud календарь не настроен.\n{h(msg)}",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="nav:add")]]
            ),
            parse_mode="HTML",
        )

    await state.clear()
    await state.update_data(wizard_chat_id=int(callback.message.chat.id), wizard_msg_id=int(callback.message.message_id))
    await state.set_state(AddEventWizard.choosing_kind)
    await wizard_render(
        bot=callback.bot,
        state=state,
        chat_id=int(callback.message.chat.id),
        fallback_msg=callback.message,
        text="📅 <b>Событие</b>: выберите календарь",
        reply_markup=event_kind_kb(),
        parse_mode="HTML",
    )


async def cb_event_choose_kind(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if not await _guard(callback, deps):
        return
    await callback.answer()

    kind = callback.data.split(":", 2)[2]
    if kind not in {"work", "personal"}:
        return

    project_id = None
    project_code = None
    include_project = False

    if kind == "work":
        try:
            async with db_pool.acquire() as conn:
                project_id = await get_current_project_id(conn, int(callback.message.chat.id))
                if project_id:
                    row = await conn.fetchrow("SELECT code FROM projects WHERE id=$1", int(project_id))
                    if row:
                        project_code = row["code"]
        except Exception:
            project_id = None
            project_code = None

    await state.update_data(
        kind=kind,
        project_id=project_id,
        project_code=project_code,
        include_project=include_project,
    )
    await state.set_state(AddEventWizard.entering_title)

    title_hint = "Введите название события одной строкой."
    if kind == "work" and project_code:
        title_hint += "\n\nМожно добавить текущий проект в название кнопкой ниже."

    await wizard_render(
        bot=callback.bot,
        state=state,
        chat_id=int(callback.message.chat.id),
        fallback_msg=callback.message,
        text=title_hint,
        reply_markup=event_title_kb(kind, project_code, include_project),
        parse_mode="HTML",
    )


async def cb_event_toggle_project(callback: CallbackQuery, state: FSMContext, deps: AppDeps) -> None:
    if not await _guard(callback, deps):
        return
    await callback.answer()

    data = await state.get_data()

    kind = data.get("kind") or "work"
    project_code = data.get("project_code")
    include_project = not bool(data.get("include_project", False))
    await state.update_data(include_project=include_project)

    title_hint = "Введите название события одной строкой."
    if kind == "work" and project_code:
        title_hint += f"\n\nТекущий проект: {project_code} ({'будет добавлен' if include_project else 'не будет добавлен'} в название)."

    await wizard_render(
        bot=callback.bot,
        state=state,
        chat_id=int(callback.message.chat.id),
        fallback_msg=callback.message,
        text=title_hint,
        reply_markup=event_title_kb(kind, project_code, include_project),
        parse_mode="HTML",
    )


async def msg_event_title(message: Message, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if deps.admin_id and (not message.from_user or message.from_user.id != deps.admin_id):
        return
    # Allow menu/commands during the wizard.
    if await escape_hatch_menu_or_command(message, state, db_pool):
        return
    await try_delete_user_message(message)

    title_raw = (message.text or "").strip()
    data = await state.get_data()

    kind = data.get("kind") or "work"
    project_code = data.get("project_code")
    include_project = bool(data.get("include_project", False))

    if not title_raw:
        return await wizard_render(
            bot=message.bot,
            state=state,
            chat_id=int(message.chat.id),
            fallback_msg=None,
            text="Введите непустое название.",
            reply_markup=event_title_kb(kind, project_code, include_project),
            parse_mode="HTML",
        )

    tz_name = deps.tz_name
    tz = _tz_from_deps(deps)

    # Quick Add: parse datetime & duration in the same line.
    dt = quick_parse_datetime_ru(title_raw, tz_name)
    dur = quick_parse_duration_min(title_raw)

    await state.update_data(title=title_raw)

    if dt is not None:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        dt_local = dt.astimezone(tz)
        await state.update_data(date=dt_local.date().isoformat(), time=f"{dt_local.hour:02d}:{dt_local.minute:02d}")
        if dur is not None:
            await state.update_data(duration_min=int(dur))
            await state.set_state(AddEventWizard.confirming)
            return await _event_render_confirm(message, state, deps=deps)

        await state.set_state(AddEventWizard.choosing_duration)
        return await wizard_render(
            bot=message.bot,
            state=state,
            chat_id=int(message.chat.id),
            fallback_msg=None,
            text="Дата распознана. Укажите длительность события:",
            reply_markup=event_duration_kb(),
            parse_mode="HTML",
        )

    await state.set_state(AddEventWizard.choosing_date)
    return await wizard_render(
        bot=message.bot,
        state=state,
        chat_id=int(message.chat.id),
        fallback_msg=None,
        text="Когда событие? Выберите дату или отправьте дату/время сообщением.",
        reply_markup=event_date_kb(),
        parse_mode="HTML",
    )


async def cb_event_choose_date(callback: CallbackQuery, state: FSMContext, deps: AppDeps) -> None:
    if not await _guard(callback, deps):
        return
    await callback.answer()

    mode = callback.data.split(":", 2)[2]
    tz = _tz_from_deps(deps)
    today = datetime.now(tz).date()

    if mode == "today":
        d = today
    elif mode == "tomorrow":
        d = today + timedelta(days=1)
    elif mode == "manual":
        await state.set_state(AddEventWizard.entering_date)
        return await wizard_render(
            bot=callback.bot,
            state=state,
            chat_id=int(callback.message.chat.id),
            fallback_msg=callback.message,
            text="Введите дату (например 24.02 или 24.02.2026).",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data="ev:cancel")]]),
            parse_mode="HTML",
        )
    else:
        return

    await state.update_data(date=d.isoformat())
    await state.set_state(AddEventWizard.choosing_time)
    await wizard_render(
        bot=callback.bot,
        state=state,
        chat_id=int(callback.message.chat.id),
        fallback_msg=callback.message,
        text="Выберите время или отправьте его сообщением.",
        reply_markup=event_time_kb(),
        parse_mode="HTML",
    )


async def msg_event_date_manual(message: Message, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if deps.admin_id and (not message.from_user or message.from_user.id != deps.admin_id):
        return
    if await escape_hatch_menu_or_command(message, state, db_pool):
        return
    await try_delete_user_message(message)

    raw = (message.text or "").strip()
    tz_name = deps.tz_name
    tz = _tz_from_deps(deps)

    parsed = await asyncio.to_thread(
        dateparser.parse,
        raw,
        settings={
            "TIMEZONE": tz_name,
            "RETURN_AS_TIMEZONE_AWARE": True,
            "DATE_ORDER": "DMY",
            "PREFER_DATES_FROM": "future",
        },
        languages=["ru"],
    )

    if not parsed:
        return await wizard_render(
            bot=message.bot,
            state=state,
            chat_id=int(message.chat.id),
            fallback_msg=None,
            text="Не понял дату. Пример: 24.02 или 24.02.2026",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data="ev:cancel")]]),
            parse_mode="HTML",
        )

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    parsed_local = parsed.astimezone(tz)
    d = parsed_local.date()
    has_explicit_time = bool(re.search(r"\b\d{1,2}[:.]\d{2}\b", raw))

    await state.update_data(date=d.isoformat())
    if has_explicit_time:
        await state.update_data(time=f"{parsed_local.hour:02d}:{parsed_local.minute:02d}")
        await state.set_state(AddEventWizard.choosing_duration)
        return await wizard_render(
            bot=message.bot,
            state=state,
            chat_id=int(message.chat.id),
            fallback_msg=None,
            text="Длительность события:",
            reply_markup=event_duration_kb(),
            parse_mode="HTML",
        )

    await state.set_state(AddEventWizard.choosing_time)
    await wizard_render(
        bot=message.bot,
        state=state,
        chat_id=int(message.chat.id),
        fallback_msg=None,
        text="Выберите время или отправьте его сообщением.",
        reply_markup=event_time_kb(),
        parse_mode="HTML",
    )


async def cb_event_choose_time(callback: CallbackQuery, state: FSMContext, deps: AppDeps) -> None:
    if not await _guard(callback, deps):
        return
    await callback.answer()

    mode = callback.data.split(":", 2)[2]
    if mode == "manual":
        await state.set_state(AddEventWizard.entering_time)
        return await wizard_render(
            bot=callback.bot,
            state=state,
            chat_id=int(callback.message.chat.id),
            fallback_msg=callback.message,
            text="Введите время (например 15:30).",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data="ev:cancel")]]),
            parse_mode="HTML",
        )

    m = re.match(r"^(\d{1,2}):(\d{2})$", mode)
    if not m:
        return
    hh, mm = int(m.group(1)), int(m.group(2))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return

    await state.update_data(time=f"{hh:02d}:{mm:02d}")
    await state.set_state(AddEventWizard.choosing_duration)
    await wizard_render(
        bot=callback.bot,
        state=state,
        chat_id=int(callback.message.chat.id),
        fallback_msg=callback.message,
        text="Длительность события:",
        reply_markup=event_duration_kb(),
        parse_mode="HTML",
    )


async def msg_event_time_manual(message: Message, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if deps.admin_id and (not message.from_user or message.from_user.id != deps.admin_id):
        return
    if await escape_hatch_menu_or_command(message, state, db_pool):
        return
    await try_delete_user_message(message)

    raw = (message.text or "").strip()
    m = re.match(r"^(\d{1,2})[:.](\d{2})$", raw)
    if not m:
        return await wizard_render(
            bot=message.bot,
            state=state,
            chat_id=int(message.chat.id),
            fallback_msg=None,
            text="Не понял время. Пример: 15:30",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data="ev:cancel")]]),
            parse_mode="HTML",
        )

    hh, mm = int(m.group(1)), int(m.group(2))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return await wizard_render(
            bot=message.bot,
            state=state,
            chat_id=int(message.chat.id),
            fallback_msg=None,
            text="Некорректное время. Пример: 15:30",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data="ev:cancel")]]),
            parse_mode="HTML",
        )

    await state.update_data(time=f"{hh:02d}:{mm:02d}")
    await state.set_state(AddEventWizard.choosing_duration)
    await wizard_render(
        bot=message.bot,
        state=state,
        chat_id=int(message.chat.id),
        fallback_msg=None,
        text="Длительность события:",
        reply_markup=event_duration_kb(),
        parse_mode="HTML",
    )


async def cb_event_choose_duration(callback: CallbackQuery, state: FSMContext, deps: AppDeps) -> None:
    if not await _guard(callback, deps):
        return
    await callback.answer()

    mode = callback.data.split(":", 2)[2]
    if mode == "manual":
        await state.set_state(AddEventWizard.entering_duration)
        return await wizard_render(
            bot=callback.bot,
            state=state,
            chat_id=int(callback.message.chat.id),
            fallback_msg=callback.message,
            text="Введите длительность в минутах (например 45).",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data="ev:cancel")]]),
            parse_mode="HTML",
        )

    try:
        dur = int(mode)
    except ValueError:
        return

    if not (5 <= dur <= 12 * 60):
        return await wizard_render(
            bot=callback.bot,
            state=state,
            chat_id=int(callback.message.chat.id),
            fallback_msg=callback.message,
            text="Некорректная длительность. Пример: 60",
            reply_markup=event_duration_kb(),
            parse_mode="HTML",
        )

    await state.update_data(duration_min=int(dur))
    await state.set_state(AddEventWizard.confirming)
    await _event_render_confirm(callback.message, state, deps=deps)


async def msg_event_duration_manual(message: Message, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if deps.admin_id and (not message.from_user or message.from_user.id != deps.admin_id):
        return
    if await escape_hatch_menu_or_command(message, state, db_pool):
        return
    await try_delete_user_message(message)

    raw = (message.text or "").strip()
    try:
        dur = int(raw)
    except ValueError:
        return await wizard_render(
            bot=message.bot,
            state=state,
            chat_id=int(message.chat.id),
            fallback_msg=None,
            text="Введите число минут. Пример: 60",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data="ev:cancel")]]),
            parse_mode="HTML",
        )

    if not (5 <= dur <= 12 * 60):
        return await wizard_render(
            bot=message.bot,
            state=state,
            chat_id=int(message.chat.id),
            fallback_msg=None,
            text="Некорректная длительность. Пример: 60",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data="ev:cancel")]]),
            parse_mode="HTML",
        )

    await state.update_data(duration_min=int(dur))
    await state.set_state(AddEventWizard.confirming)
    await _event_render_confirm(message, state, deps=deps)


async def cb_event_edit_datetime(callback: CallbackQuery, state: FSMContext, deps: AppDeps) -> None:
    if not await _guard(callback, deps):
        return
    await callback.answer()

    await state.set_state(AddEventWizard.choosing_date)
    await wizard_render(
        bot=callback.bot,
        state=state,
        chat_id=int(callback.message.chat.id),
        fallback_msg=callback.message,
        text="Когда событие? Выберите дату или отправьте дату/время сообщением.",
        reply_markup=event_date_kb(),
        parse_mode="HTML",
    )


async def cb_event_edit_duration(callback: CallbackQuery, state: FSMContext, deps: AppDeps) -> None:
    if not await _guard(callback, deps):
        return
    await callback.answer()

    await state.set_state(AddEventWizard.choosing_duration)
    await wizard_render(
        bot=callback.bot,
        state=state,
        chat_id=int(callback.message.chat.id),
        fallback_msg=callback.message,
        text="Длительность события:",
        reply_markup=event_duration_kb(),
        parse_mode="HTML",
    )


async def _event_finalize_and_create(
    message: Message,
    state: FSMContext,
    db_pool: asyncpg.Pool,
    deps: AppDeps,
    *,
    duration_min: int,
) -> None:
    data = await state.get_data()

    icloud = deps.icloud

    vault = deps.vault

    tz = _tz_from_deps(deps)

    kind = data.get("kind")
    title = (data.get("title") or "").strip()
    project_id = data.get("project_id")
    project_code = data.get("project_code")
    include_project = bool(data.get("include_project", False))
    d_iso = data.get("date")
    t_hm = data.get("time")

    if not (kind and title and d_iso and t_hm):
        await wizard_render(
            bot=message.bot,
            state=state,
            chat_id=int(message.chat.id),
            fallback_msg=message,
            text="❌ Не хватает данных для создания события. Начните заново через ➕ Добавить.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="↻ Событие", callback_data="add:event")],
                    [InlineKeyboardButton(text="⬅️ Добавить", callback_data="nav:add")],
                ]
            ),
            parse_mode="HTML",
        )
        await state.clear()
        return

    d = datetime.fromisoformat(d_iso).date()
    hh, mm = map(int, t_hm.split(":", 1))
    dtstart_local = datetime(d.year, d.month, d.day, hh, mm, tzinfo=tz)
    dtend_local = dtstart_local + timedelta(minutes=int(duration_min))

    # Summary template
    work_tpl = os.getenv("ICLOUD_WORK_SUMMARY_TEMPLATE", "{project_prefix}{title}")
    personal_tpl = os.getenv("ICLOUD_PERSONAL_SUMMARY_TEMPLATE", "{title}")

    project_prefix = ""
    if kind == "work" and include_project and project_code:
        code = str(project_code)
        # Avoid double prefix
        if not title.startswith(f"{code}:"):
            project_prefix = f"{code}: "

    if kind == "work":
        cal_url = os.getenv("ICLOUD_CALENDAR_URL_WORK", "")
        if not cal_url:
            await wizard_render(
                bot=message.bot,
                state=state,
                chat_id=int(message.chat.id),
                fallback_msg=message,
                text="❌ Не задан ICLOUD_CALENDAR_URL_WORK для рабочих событий.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home")]]),
                parse_mode="HTML",
            )
            await state.clear()
            return
        summary = work_tpl.format(title=title, project=(project_code or ""), project_prefix=project_prefix)
    else:
        cal_url = os.getenv("ICLOUD_CALENDAR_URL_PERSONAL", "")
        if not cal_url:
            await wizard_render(
                bot=message.bot,
                state=state,
                chat_id=int(message.chat.id),
                fallback_msg=message,
                text="❌ Не задан ICLOUD_CALENDAR_URL_PERSONAL для личных событий.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home")]]),
                parse_mode="HTML",
            )
            await state.clear()
            return
        summary = personal_tpl.format(title=title, project="", project_prefix="")

    dtstart_utc = dtstart_local.astimezone(timezone.utc)
    dtend_utc = dtend_local.astimezone(timezone.utc)

    ics_url = ""
    try:
        ics_url, success = await icloud.create_event(
            calendar_url=cal_url,
            summary=summary,
            dtstart_utc=dtstart_utc,
            dtend_utc=dtend_utc,
        )
        if not success:
            async with db_pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO icloud_events (calendar_url, summary, dtstart_utc, dtend_utc, sync_status, last_error)
                    VALUES ($1, $2, $3, $4, 'pending', 'Initial sync failed')
                    """,
                    cal_url,
                    summary,
                    dtstart_utc,
                    dtend_utc,
                )
            await wizard_render(
                bot=message.bot,
                state=state,
                chat_id=int(message.chat.id),
                fallback_msg=message,
                text="⚠️ Не удалось создать событие в iCloud Calendar. Событие сохранено локально и будет синхронизировано позже.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home")]]),
                parse_mode="HTML",
            )
            await state.clear()
            return
    except Exception as e:
        try:
            async with db_pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO icloud_events (calendar_url, summary, dtstart_utc, dtend_utc, sync_status, last_error)
                    VALUES ($1, $2, $3, $4, 'pending', $5)
                    """,
                    cal_url,
                    summary,
                    dtstart_utc,
                    dtend_utc,
                    str(e)[:500],
                )
        except Exception:
            pass

        await wizard_render(
            bot=message.bot,
            state=state,
            chat_id=int(message.chat.id),
            fallback_msg=message,
            text="⚠️ Не удалось создать событие в iCloud Calendar. Событие сохранено локально и будет синхронизировано позже.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home")]]),
            parse_mode="HTML",
        )
        await state.clear()
        return

    # History log (best-effort)
    try:
        async with db_pool.acquire() as conn:
            txt = f"📅 Событие: {_fmt_local(dtstart_local, tz)} ({int(duration_min)} мин) — {summary}"
            if ics_url:
                txt += f"\n{ics_url}"
            await db_add_event(
                conn,
                event_type="ical_event_created",
                project_id=int(project_id) if (kind == "work" and project_id) else None,
                task_id=None,
                text=txt,
            )
    except Exception:
        pass

    # Work event may affect project history in Vault
    if kind == "work" and project_id and vault is not None:
        fire_and_forget(
            background_project_sync(
                int(project_id),
                db_pool,
                vault,
                error_logger=lambda w, e, c: db_log_error(db_pool, w, e, c),
            ),
            label="icloud-event-sync",
        )

    when_txt = _fmt_local(dtstart_local, tz)
    await wizard_render(
        bot=message.bot,
        state=state,
        chat_id=int(message.chat.id),
        fallback_msg=message,
        text=f"✅ Событие создано: <b>{h(when_txt)}</b> ({int(duration_min)} мин)\n\n{h(summary)}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home")]]),
        parse_mode="HTML",
    )
    await state.clear()


async def cb_event_create(
    callback: CallbackQuery,
    state: FSMContext,
    db_pool: asyncpg.Pool,
    deps: AppDeps,
) -> None:
    if not await _guard(callback, deps):
        return
    await callback.answer()

    data = await state.get_data()

    dur = data.get("duration_min")
    try:
        dur_i = int(dur)
    except Exception:
        dur_i = 0
    if dur_i <= 0:
        await state.set_state(AddEventWizard.choosing_duration)
        return await wizard_render(
            bot=callback.bot,
            state=state,
            chat_id=int(callback.message.chat.id),
            fallback_msg=callback.message,
            text="Укажите длительность события:",
            reply_markup=event_duration_kb(),
            parse_mode="HTML",
        )

    await _event_finalize_and_create(callback.message, state, db_pool, deps, duration_min=dur_i)


async def cb_event_cancel(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if not await _guard(callback, deps):
        return
    await callback.answer()
    await state.clear()
    await ui_render_home(callback.message, db_pool, tz_name=deps.tz_name, force_new=False)


def register(dp: Dispatcher) -> None:
    dp.callback_query.register(cb_add_event_start, F.data == "add:event")

    dp.callback_query.register(cb_event_choose_kind, StateFilter(AddEventWizard.choosing_kind), F.data.startswith("ev:kind:"))

    dp.callback_query.register(cb_event_toggle_project, StateFilter(AddEventWizard.entering_title), F.data == "ev:proj:toggle")
    dp.message.register(msg_event_title, StateFilter(AddEventWizard.entering_title), F.text)

    dp.callback_query.register(cb_event_choose_date, StateFilter(AddEventWizard.choosing_date), F.data.startswith("ev:date:"))
    dp.message.register(msg_event_date_manual, StateFilter(AddEventWizard.choosing_date), F.text)
    dp.message.register(msg_event_date_manual, StateFilter(AddEventWizard.entering_date), F.text)

    dp.callback_query.register(cb_event_choose_time, StateFilter(AddEventWizard.choosing_time), F.data.startswith("ev:time:"))
    dp.message.register(msg_event_time_manual, StateFilter(AddEventWizard.choosing_time), F.text)
    dp.message.register(msg_event_time_manual, StateFilter(AddEventWizard.entering_time), F.text)

    dp.callback_query.register(cb_event_choose_duration, StateFilter(AddEventWizard.choosing_duration), F.data.startswith("ev:dur:"))
    dp.message.register(msg_event_duration_manual, StateFilter(AddEventWizard.entering_duration), F.text)

    dp.callback_query.register(cb_event_edit_datetime, StateFilter(AddEventWizard.confirming), F.data == "ev:edit_datetime")
    dp.callback_query.register(cb_event_edit_duration, StateFilter(AddEventWizard.confirming), F.data == "ev:edit_duration")
    dp.callback_query.register(cb_event_create, StateFilter(AddEventWizard.confirming), F.data == "ev:create")

    dp.callback_query.register(cb_event_cancel, F.data == "ev:cancel")
