"""Minimal daily-use UI for the Telegram assistant.

The legacy screens remain available for compatibility, but normal navigation is
reduced to three surfaces: tasks, today, and a small secondary menu.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import asyncpg
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.adapters.icloud_caldav_adapter import ICloudCalDAVAdapter
from bot.tz import resolve_tz_name, resolve_tzinfo
from bot.ui.render import ui_render
from bot.utils import h
import bot.ui.screens as legacy


UTC = timezone.utc


def _task_rows_without_inbox_label(rows) -> list[dict]:
    result: list[dict] = []
    for raw in rows or []:
        row = dict(raw)
        if str(row.get("project") or "").strip().upper() == "INBOX":
            row["project"] = ""
        result.append(row)
    return result


def _qd_suffix(quick_done: bool) -> str:
    return ":qd1" if quick_done else ""


def _short_task_title(value: object, limit: int = 76) -> str:
    """Keep the list scannable; the task card still shows the full title."""
    text = " ".join(str(value or "").split()).strip() or "Без названия"
    if len(text) <= limit:
        return text
    cut = text[: max(1, limit - 1)].rstrip()
    if " " in cut:
        word_cut = cut.rsplit(" ", 1)[0].rstrip()
        if len(word_cut) >= limit // 2:
            cut = word_cut
    return f"{cut}…"


def _daily_task_lines_and_buttons(rows, tz, *, offset: int = 0, quick_done: bool = False):
    """Render action first and keep project/deadline/assignee as quiet metadata."""
    lines: list[str] = []
    buttons: list[InlineKeyboardButton] = []
    for number, row in enumerate(rows or [], offset + 1):
        title = _short_task_title(row.get("title"))
        lines.append(f"<b>{number}. {h(title)}</b>")

        meta: list[str] = []
        project = str(row.get("project") or "").strip()
        if project:
            meta.append(project)
        deadline = legacy.to_local(row.get("deadline"), tz)
        if deadline:
            meta.append(f"до {deadline.strftime('%d.%m %H:%M')}")
        assignee = str(row.get("assignee") or "").strip()
        if assignee and assignee != "—":
            meta.append(assignee)
        if meta:
            lines.append(f"<i>{h(' · '.join(meta))}</i>")

        buttons.append(
            InlineKeyboardButton(
                text=f"✓ {number}" if quick_done else str(number),
                callback_data=(
                    f"task:{int(row['id'])}:done_quick"
                    if quick_done
                    else f"task:{int(row['id'])}"
                ),
            )
        )
    return lines, [buttons[i:i + 4] for i in range(0, len(buttons), 4)]


async def ui_render_home(
    message: Message | None,
    db_pool: asyncpg.Pool,
    *,
    tz_name: str | None = None,
    preferred_message_id: int | None = None,
    force_new: bool = False,
) -> int:
    if message is None:
        return 0
    return await ui_render_all_tasks(
        message,
        db_pool,
        tz_name=tz_name,
        page=0,
        quick_done=False,
        preferred_message_id=preferred_message_id,
        force_new=force_new,
    )


async def ui_render_all_tasks(
    message: Message,
    db_pool: asyncpg.Pool,
    *,
    tz_name: str | None = None,
    page: int = 0,
    filter_key: str = "all",
    quick_done: bool = False,
    preferred_message_id: int | None = None,
    force_new: bool = False,
) -> int:
    """Render one urgency-sorted work list; legacy filter_key is ignored."""
    del filter_key
    tz_name = resolve_tz_name(tz_name or "Europe/Moscow")
    tz = resolve_tzinfo(tz_name)
    chat_id = int(message.chat.id)
    toast_line = await legacy._pop_screen_toast(db_pool, chat_id)

    page_size = 20
    try:
        page = max(0, int(page or 0))
    except Exception:
        page = 0

    now_local = datetime.now(tz)
    tomorrow_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    now_utc = datetime.now(UTC).replace(tzinfo=None)
    tomorrow_utc = tomorrow_local.astimezone(UTC).replace(tzinfo=None)

    try:
        async with db_pool.acquire() as conn:
            total = int(
                await conn.fetchval(
                    """
                    SELECT COUNT(*)
                    FROM tasks t
                    JOIN projects p ON p.id=t.project_id
                    WHERE t.status NOT IN ('done','postponed')
                      AND t.kind != 'super'
                      AND p.status='active'
                    """
                )
                or 0
            )
            max_page = max(0, (total - 1) // page_size) if total else 0
            page = min(page, max_page)
            rows = await conn.fetch(
                """
                SELECT t.id, t.title, p.code AS project,
                       COALESCE(tm.name,'—') AS assignee, t.deadline,
                       t.status, t.created_at
                FROM tasks t
                JOIN projects p ON p.id=t.project_id
                LEFT JOIN team tm ON tm.id=t.assignee_id
                WHERE t.status NOT IN ('done','postponed')
                  AND t.kind != 'super'
                  AND p.status='active'
                ORDER BY
                  CASE
                    WHEN t.deadline IS NOT NULL AND t.deadline < $1 THEN 0
                    WHEN t.deadline IS NOT NULL AND t.deadline < $2 THEN 1
                    WHEN t.deadline IS NOT NULL THEN 2
                    ELSE 3
                  END,
                  t.deadline ASC NULLS LAST,
                  CASE WHEN t.status='in_progress' THEN 0 ELSE 1 END,
                  t.created_at ASC,
                  t.id ASC
                LIMIT $3 OFFSET $4
                """,
                now_utc,
                tomorrow_utc,
                page_size,
                page * page_size,
            )
    except Exception as exc:
        return await ui_render(
            bot=message.bot,
            db_pool=db_pool,
            chat_id=chat_id,
            text=f"Не удалось открыть дела: {h(str(exc))}",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Повторить", callback_data="nav:home")]]
            ),
            screen="all_tasks",
            fallback_message=message,
            preferred_message_id=preferred_message_id,
            force_new=force_new,
            parse_mode="HTML",
        )

    rows = _task_rows_without_inbox_label(rows)
    lines: list[str] = [f"<b>Дела · {total}</b>"]
    if page > 0 and rows:
        start = page * page_size + 1
        lines.append(f"<i>{start}–{start + len(rows) - 1}</i>")
    if toast_line:
        lines = [toast_line, ""] + lines

    kb: list[list[InlineKeyboardButton]] = []
    if not rows:
        lines.extend(["", "Список пуст. Просто напишите или надиктуйте новое дело."])
    else:
        task_lines, task_buttons = _daily_task_lines_and_buttons(
            rows,
            tz,
            offset=page * page_size,
            quick_done=quick_done,
        )
        lines.extend(["", *task_lines])
        kb.extend(task_buttons)
        kb.append(
            [
                InlineKeyboardButton(
                    text="Открывать" if quick_done else "✓ Готово",
                    callback_data=(
                        f"nav:all:all:{page}:qd0"
                        if quick_done
                        else f"nav:all:all:{page}:qd1"
                    ),
                )
            ]
        )

    nav_row: list[InlineKeyboardButton] = []
    suffix = _qd_suffix(quick_done)
    if page > 0:
        nav_row.append(
            InlineKeyboardButton(text="←", callback_data=f"nav:all:all:{page-1}{suffix}")
        )
    if (page + 1) * page_size < total:
        nav_row.append(
            InlineKeyboardButton(text="→", callback_data=f"nav:all:all:{page+1}{suffix}")
        )
    if nav_row:
        kb.append(nav_row)

    kb.append(
        [
            InlineKeyboardButton(text="Сегодня", callback_data="nav:today"),
            InlineKeyboardButton(text="⋯", callback_data="nav:secondary"),
        ]
    )

    return await ui_render(
        bot=message.bot,
        db_pool=db_pool,
        chat_id=chat_id,
        text="\n".join(lines).strip(),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
        screen="all_tasks",
        payload={"page": page, "filter": "all", "quick_done": quick_done},
        fallback_message=message,
        preferred_message_id=preferred_message_id,
        force_new=force_new,
        parse_mode="HTML",
    )


async def ui_render_today(
    message: Message,
    db_pool: asyncpg.Pool,
    *,
    tz_name: str | None = None,
    page: int = 0,
    icloud: ICloudCalDAVAdapter | None = None,
    preferred_message_id: int | None = None,
    force_new: bool = False,
) -> int:
    tz_name = resolve_tz_name(tz_name or "Europe/Moscow")
    tz = resolve_tzinfo(tz_name)
    chat_id = int(message.chat.id)
    toast_line = await legacy._pop_screen_toast(db_pool, chat_id)
    page_size = 10
    page = max(0, int(page or 0))

    work_calendar_url = (os.getenv("ICLOUD_CALENDAR_URL_WORK") or "").strip()
    personal_calendar_url = (os.getenv("ICLOUD_CALENDAR_URL_PERSONAL") or "").strip()
    bitrix_calendar_url = (os.getenv("ICLOUD_CALENDAR_URL_BITRIX") or "").strip()

    try:
        calendar_block = await legacy._fetch_today_calendar_block(
            tz=tz,
            calendar_urls=[work_calendar_url, personal_calendar_url, bitrix_calendar_url],
            icloud=icloud,
        )
        async with db_pool.acquire() as conn:
            total_tasks = int(
                await conn.fetchval(
                    """
                    SELECT COUNT(*)
                    FROM tasks t
                    WHERE t.status NOT IN ('done','postponed')
                      AND t.kind != 'super'
                      AND t.deadline IS NOT NULL
                      AND (t.deadline AT TIME ZONE 'UTC' AT TIME ZONE $1)::date =
                          (now() AT TIME ZONE $1)::date
                    """,
                    tz_name,
                )
                or 0
            )
            max_page = max(0, (total_tasks - 1) // page_size) if total_tasks else 0
            page = min(page, max_page)
            tasks = await conn.fetch(
                """
                SELECT t.id, t.title, p.code AS project,
                       COALESCE(tm.name,'—') AS assignee, t.deadline
                FROM tasks t
                JOIN projects p ON p.id=t.project_id
                LEFT JOIN team tm ON tm.id=t.assignee_id
                WHERE t.status NOT IN ('done','postponed')
                  AND t.kind != 'super'
                  AND t.deadline IS NOT NULL
                  AND (t.deadline AT TIME ZONE 'UTC' AT TIME ZONE $1)::date =
                      (now() AT TIME ZONE $1)::date
                ORDER BY t.deadline ASC, t.id ASC
                LIMIT $2 OFFSET $3
                """,
                tz_name,
                page_size,
                page * page_size,
            )
            reminders = await conn.fetch(
                """
                SELECT id, text, remind_at
                FROM reminders
                WHERE is_sent=FALSE
                  AND cancelled_at_utc IS NULL
                  AND (remind_at AT TIME ZONE 'UTC' AT TIME ZONE $1)::date =
                      (now() AT TIME ZONE $1)::date
                ORDER BY remind_at ASC
                """,
                tz_name,
            )
    except Exception as exc:
        return await ui_render(
            bot=message.bot,
            db_pool=db_pool,
            chat_id=chat_id,
            text=f"Не удалось открыть сегодня: {h(str(exc))}",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Дела", callback_data="nav:home")]]
            ),
            screen="today",
            fallback_message=message,
            preferred_message_id=preferred_message_id,
            force_new=force_new,
            parse_mode="HTML",
        )

    tasks = _task_rows_without_inbox_label(tasks)
    reminders = [dict(row) for row in reminders or []]
    events = list(calendar_block.events)
    now_local = datetime.now(tz)

    lines: list[str] = [f"<b>Сегодня · {now_local.strftime('%d.%m')}</b>"]
    if toast_line:
        lines = [toast_line, ""] + lines

    if events:
        lines.extend(["", "<b>Календарь</b>"])
        for event in events:
            start_local = legacy._event_local(event.dtstart_utc, tz)
            end_local = legacy._event_local(event.dtend_utc, tz)
            if start_local and end_local:
                when = f"{start_local.strftime('%H:%M')}–{end_local.strftime('%H:%M')}"
            elif start_local:
                when = start_local.strftime("%H:%M")
            else:
                when = "—"
            lines.append(f"<b>{h(when)}</b>  {h(event.summary or 'Без названия')}")

    if reminders:
        lines.extend(["", "<b>Напоминания</b>"])
        for reminder in reminders:
            dt_local = legacy.to_local(reminder.get("remind_at"), tz)
            when = dt_local.strftime("%H:%M") if dt_local else "—"
            lines.append(f"<b>{h(when)}</b>  {h(str(reminder.get('text') or ''))}")

    kb: list[list[InlineKeyboardButton]] = []
    if tasks:
        lines.extend(["", "<b>Дела</b>"])
        task_lines, task_buttons = _daily_task_lines_and_buttons(
            tasks,
            tz,
            offset=page * page_size,
        )
        lines.extend(task_lines)
        kb.extend(task_buttons)

    if not events and not reminders and not tasks:
        lines.extend(["", "На сегодня ничего обязательного."])

    if calendar_block.unavailable:
        lines.extend(["", "<i>Часть календаря сейчас недоступна.</i>"])

    page_row: list[InlineKeyboardButton] = []
    if page > 0:
        page_row.append(InlineKeyboardButton(text="←", callback_data=f"nav:today:{page-1}"))
    if (page + 1) * page_size < total_tasks:
        page_row.append(InlineKeyboardButton(text="→", callback_data=f"nav:today:{page+1}"))
    if page_row:
        kb.append(page_row)

    kb.append(
        [
            InlineKeyboardButton(text="Дела", callback_data="nav:home"),
            InlineKeyboardButton(text="⋯", callback_data="nav:secondary"),
        ]
    )

    return await ui_render(
        bot=message.bot,
        db_pool=db_pool,
        chat_id=chat_id,
        text="\n".join(lines).strip(),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
        screen="today",
        payload={"page": page},
        fallback_message=message,
        preferred_message_id=preferred_message_id,
        force_new=force_new,
        parse_mode="HTML",
    )


async def ui_render_home_more(
    message: Message,
    db_pool: asyncpg.Pool,
    *,
    preferred_message_id: int | None = None,
    force_new: bool = False,
) -> int:
    chat_id = int(message.chat.id)
    toast_line = await legacy._pop_screen_toast(db_pool, chat_id)
    lines = ["<b>Ещё</b>"]
    if toast_line:
        lines = [toast_line, ""] + lines

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Проекты", callback_data="nav:projects"),
                InlineKeyboardButton(text="Напоминания", callback_data="nav:reminders:0"),
            ],
            [InlineKeyboardButton(text="Помощь", callback_data="nav:help")],
            [InlineKeyboardButton(text="Дела", callback_data="nav:home")],
        ]
    )
    return await ui_render(
        bot=message.bot,
        db_pool=db_pool,
        chat_id=chat_id,
        text="\n".join(lines),
        reply_markup=kb,
        screen="secondary",
        fallback_message=message,
        preferred_message_id=preferred_message_id,
        force_new=force_new,
        parse_mode="HTML",
    )


async def ui_render_help(
    message: Message,
    db_pool: asyncpg.Pool,
    *,
    preferred_message_id: int | None = None,
    force_new: bool = False,
) -> int:
    toast_line = await legacy._pop_screen_toast(db_pool, int(message.chat.id))
    lines = [
        "<b>Как пользоваться</b>",
        "",
        "Просто пишите или диктуйте то, что нужно не забыть.",
        "",
        "«Проверить расчёт осадки»",
        "«До пятницы отправить расчёт заказчику»",
        "«Напомни завтра в 10 позвонить Иванову»",
        "",
        "Несколько дел можно отправить одним сообщением. Проект и срок указывать необязательно.",
    ]
    if toast_line:
        lines = [toast_line, ""] + lines
    return await ui_render(
        bot=message.bot,
        db_pool=db_pool,
        chat_id=int(message.chat.id),
        text="\n".join(lines),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Дела", callback_data="nav:home")]]
        ),
        screen="help",
        fallback_message=message,
        preferred_message_id=preferred_message_id,
        force_new=force_new,
        parse_mode="HTML",
    )


async def ui_render_add_menu(
    message: Message,
    db_pool: asyncpg.Pool,
    *,
    preferred_message_id: int | None = None,
    force_new: bool = False,
) -> int:
    """Keep old /add entry points safe without making forms part of navigation."""
    toast_line = await legacy._pop_screen_toast(db_pool, int(message.chat.id))
    lines = [
        "<b>Добавление</b>",
        "",
        "Ничего выбирать не нужно — напишите или надиктуйте дело прямо в чат.",
        "Если нужно загрузить большой список, можно открыть импорт ниже.",
    ]
    if toast_line:
        lines = [toast_line, ""] + lines
    return await ui_render(
        bot=message.bot,
        db_pool=db_pool,
        chat_id=int(message.chat.id),
        text="\n".join(lines),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Добавить списком", callback_data="onboard:start")],
                [InlineKeyboardButton(text="Дела", callback_data="nav:home")],
            ]
        ),
        screen="add",
        fallback_message=message,
        preferred_message_id=preferred_message_id,
        force_new=force_new,
        parse_mode="HTML",
    )
