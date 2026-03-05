"""High-level SPA screens (renderers).

These functions render whole "screens" (Home/Projects/Today/Overdue/Help/Add),
updating the single UI message for the chat via :func:`bot.ui.render.ui_render`.
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from bot.tz import resolve_tz_name, resolve_tzinfo

import asyncpg
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from bot.ui.render import ui_render
from bot.ui.state import ui_get_state, ui_set_state, _ui_payload_get, _undo_active, _now_ts
from bot.utils import h, fmt_task_line_html, kb_columns
from bot.keyboards import home_kb, back_home_kb, add_menu_kb, today_screen_kb, main_menu_kb


async def ensure_main_menu(message: Message, db_pool: asyncpg.Pool) -> None:
    """Ensure the persistent bottom reply-keyboard is visible.

    Telegram clients may hide reply keyboards; the most reliable way to restore
    it is to (re)send a message that carries the ReplyKeyboardMarkup. To avoid
    chat spam, we keep a single 'anchor' message id in DB and try to edit it;
    if it was deleted, we create a new one and store its id.
    """

    chat_id = int(message.chat.id)

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT menu_message_id FROM user_settings WHERE chat_id=$1",
            chat_id,
        )
        menu_mid = row["menu_message_id"] if row else None

    # Use visually blank text so anchor doesn't distract.
    _ANCHOR_TEXT_A = "ㅤ"  # Hangul filler (renders as blank)
    _ANCHOR_TEXT_B = "ㅤㅤ"

    if menu_mid:
        try:
            await message.bot.edit_message_text(
                chat_id=chat_id,
                message_id=int(menu_mid),
                text=_ANCHOR_TEXT_A,
                reply_markup=main_menu_kb(),
            )
            return
        except TelegramBadRequest as e:
            if "message is not modified" in str(e).lower():
                try:
                    await message.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=int(menu_mid),
                        text=_ANCHOR_TEXT_B,
                        reply_markup=main_menu_kb(),
                    )
                    return
                except Exception:
                    return
        except TelegramRetryAfter as e:
            await asyncio.sleep(float(getattr(e, "retry_after", 1.0)) + 0.1)
        except Exception:
            pass

    try:
        anchor = await message.answer(_ANCHOR_TEXT_A, reply_markup=main_menu_kb())
        async with db_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO user_settings(chat_id, menu_message_id, updated_at) VALUES($1,$2,NOW()) "
                "ON CONFLICT(chat_id) DO UPDATE SET menu_message_id=EXCLUDED.menu_message_id, updated_at=NOW()",
                chat_id,
                int(anchor.message_id),
            )
    except Exception:
        return


def _tz_name() -> str:
    return resolve_tz_name("Europe/Moscow")


UTC = timezone.utc


def to_utc(dt: datetime | None) -> datetime | None:
    """Normalize datetime to timezone-aware UTC. Treat naive as UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def to_local(dt: datetime | None, tz: ZoneInfo) -> datetime | None:
    d = to_utc(dt)
    if d is None:
        return None
    return d.astimezone(tz)


def fmt_local(dt: datetime | None, tz: ZoneInfo) -> str:
    d = to_local(dt, tz)
    return d.strftime("%d.%m %H:%M") if d else "—"


async def ui_render_home(message: Message | None, db_pool: asyncpg.Pool, *, tz_name: str | None = None, force_new: bool = False) -> None:
    """Render GTD Focus Home screen (lightweight)."""
    if not message:
        return

    chat_id = int(message.chat.id)
    tz_name = tz_name or _tz_name()
    tz = resolve_tzinfo(tz_name)

    # UTC naive for DB comparisons (pool init sets session TZ=UTC, supports TIMESTAMP and TIMESTAMPTZ)
    now_utc_naive = datetime.now(UTC).replace(tzinfo=None)

    # Local day bounds -> UTC naive
    now_local = datetime.now(tz)
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)
    start_utc_naive = start_local.astimezone(UTC).replace(tzinfo=None)
    end_utc_naive = end_local.astimezone(UTC).replace(tzinfo=None)

    def _due_str(dt_local: datetime | None, mode: str) -> str:
        if not dt_local:
            return "без срока"
        if mode == "overdue":
            return f"был {dt_local.strftime('%d.%m %H:%M')}"
        if mode == "today":
            return f"до {dt_local.strftime('%H:%M')}"
        # work
        return f"до {dt_local.strftime('%d.%m %H:%M')}"

    def _preview_lines(marker: str, project: str, title: str, assignee: str, dt_local: datetime | None, mode: str) -> list[str]:
        proj = (project or "—").strip()
        t = (title or "").strip()
        a = (assignee or "—").strip()
        # keep lines readable
        t_show = t if len(t) <= 90 else (t[:89] + "…")
        due = _due_str(dt_local, mode)
        if len(t) > 40:
            return [
                f"{marker} <b>[{h(proj)}]</b> {h(t_show)}",
                f"   {h(a)} → <i>{h(due)}</i>",
            ]
        return [f"{marker} <b>[{h(proj)}]</b> {h(t_show)} — {h(a)}, <i>{h(due)}</i>"]

    try:
        async with db_pool.acquire() as conn:
            ui_state = await ui_get_state(conn, chat_id)
            payload = _ui_payload_get(ui_state)

            # Toast notification (one-shot)
            toast_line = None
            toast = payload.get("toast") if isinstance(payload, dict) else None
            if isinstance(toast, dict):
                exp = int(toast.get("exp") or 0)
                if not exp or exp >= _now_ts():
                    ttxt = (toast.get("text") or "").strip()
                    if ttxt:
                        toast_line = ttxt
                payload.pop("toast", None)

            # Context: current project + INBOX
            current_project_id = await conn.fetchval(
                "SELECT current_project_id FROM user_settings WHERE chat_id=$1",
                chat_id,
            )
            current_project_code = "—"
            current_project_is_inbox = False
            if current_project_id:
                row = await conn.fetchrow("SELECT code FROM projects WHERE id=$1", int(current_project_id))
                if row and row.get("code"):
                    current_project_code = str(row["code"])
                    current_project_is_inbox = current_project_code.upper() == "INBOX"

            inbox_id = await conn.fetchval("SELECT id FROM projects WHERE code='INBOX' LIMIT 1")

            # Counts
            overdue_count = await conn.fetchval(
                "SELECT COUNT(*) FROM tasks WHERE status NOT IN ('done','postponed') AND deadline IS NOT NULL AND deadline < $1",
                now_utc_naive,
            )
            today_count = await conn.fetchval(
                "SELECT COUNT(*) FROM tasks WHERE status NOT IN ('done','postponed') AND deadline IS NOT NULL AND deadline >= $1 AND deadline < $2",
                start_utc_naive,
                end_utc_naive,
            )
            inbox_count = 0
            if inbox_id:
                inbox_count = await conn.fetchval(
                    "SELECT COUNT(*) FROM tasks WHERE status != 'done' AND project_id=$1",
                    int(inbox_id),
                )

            # Work (in_progress): focus within current project if set and not INBOX; otherwise global.
            work_where = "t.status='in_progress'"
            args: list = []
            if current_project_id and not current_project_is_inbox:
                work_where += " AND t.project_id=$1"
                args = [int(current_project_id)]

            work_count = await conn.fetchval(f"SELECT COUNT(*) FROM tasks t WHERE {work_where}", *args) if args else await conn.fetchval(f"SELECT COUNT(*) FROM tasks t WHERE {work_where}")

            # Previews
            overdue_rows = await conn.fetch(
                """
                SELECT t.id, t.title, p.code AS project, COALESCE(tm.name,'—') AS assignee, t.deadline
                FROM tasks t
                JOIN projects p ON p.id=t.project_id
                LEFT JOIN team tm ON tm.id=t.assignee_id
                WHERE t.status NOT IN ('done','postponed') AND t.deadline IS NOT NULL AND t.deadline < $1
                ORDER BY t.deadline ASC
                LIMIT 3
                """,
                now_utc_naive,
            )

            today_rows = await conn.fetch(
                """
                SELECT t.id, t.title, p.code AS project, COALESCE(tm.name,'—') AS assignee, t.deadline
                FROM tasks t
                JOIN projects p ON p.id=t.project_id
                LEFT JOIN team tm ON tm.id=t.assignee_id
                WHERE t.status NOT IN ('done','postponed') AND t.deadline IS NOT NULL AND t.deadline >= $1 AND t.deadline < $2
                ORDER BY t.deadline ASC
                LIMIT 3
                """,
                start_utc_naive,
                end_utc_naive,
            )

            if args:
                work_rows = await conn.fetch(
                    f"""
                    SELECT t.id, t.title, p.code AS project, COALESCE(tm.name,'—') AS assignee, t.deadline
                    FROM tasks t
                    JOIN projects p ON p.id=t.project_id
                    LEFT JOIN team tm ON tm.id=t.assignee_id
                    WHERE {work_where}
                    ORDER BY t.deadline ASC NULLS LAST, t.created_at DESC
                    LIMIT 3
                    """,
                    *args,
                )
            else:
                work_rows = await conn.fetch(
                    f"""
                    SELECT t.id, t.title, p.code AS project, COALESCE(tm.name,'—') AS assignee, t.deadline
                    FROM tasks t
                    JOIN projects p ON p.id=t.project_id
                    LEFT JOIN team tm ON tm.id=t.assignee_id
                    WHERE {work_where}
                    ORDER BY t.deadline ASC NULLS LAST, t.created_at DESC
                    LIMIT 3
                    """
                )

            # Persist payload without toast
            await ui_set_state(conn, chat_id, ui_payload=payload)

        # Build text
        lines: list[str] = []
        if toast_line:
            lines.extend([toast_line, ""])

        lines.append("🧠 <b>Фокус</b>")
        lines.append(f"⭐ Проект: <b>{h(str(current_project_code))}</b>")
        lines.append("")
        lines.append(f"🔥 Срочно: <b>{int(overdue_count or 0)}</b>")
        lines.append(f"⏰ Сегодня: <b>{int(today_count or 0)}</b>")
        lines.append(f"⚡ В работе: <b>{int(work_count or 0)}</b>")
        lines.append(f"📥 Inbox: <b>{int(inbox_count or 0)}</b>")

        # Sections
        lines.append("")
        lines.append("<b>🔥 СРОЧНО</b>")
        if overdue_rows:
            for r in overdue_rows:
                dt_local = to_local(r.get("deadline"), tz)
                lines.extend(_preview_lines("🔥", r.get("project") or "", r.get("title") or "", r.get("assignee") or "—", dt_local, "overdue"))
        else:
            lines.append("—")

        lines.append("")
        lines.append("<b>⏰ СЕГОДНЯ</b>")
        if today_rows:
            for r in today_rows:
                dt_local = to_local(r.get("deadline"), tz)
                lines.extend(_preview_lines("⏰", r.get("project") or "", r.get("title") or "", r.get("assignee") or "—", dt_local, "today"))
        else:
            lines.append("—")

        lines.append("")
        lines.append("<b>⚡ В РАБОТЕ</b>")
        if work_rows:
            for r in work_rows:
                dt_local = to_local(r.get("deadline"), tz)
                lines.extend(_preview_lines("⚡", r.get("project") or "", r.get("title") or "", r.get("assignee") or "—", dt_local, "work"))
        else:
            lines.append("—")

        # Keyboard (dynamic)
        kb: list[list[InlineKeyboardButton]] = []

        # Row 1: quick capture (GTD).
        # Note: "Проекты" есть в нижней ReplyKeyboard, поэтому не дублируем на главной.
        kb.append([
            InlineKeyboardButton(text="⚡️ Быстрая задача", callback_data="quick:task"),
            InlineKeyboardButton(text="💡 Идея", callback_data="quick:idea"),
        ])

        # Row 2: inbox processing / inbox access
        if inbox_id and int(inbox_count or 0) > 0:
            kb.append([
                InlineKeyboardButton(text="🧹 Разобрать Inbox", callback_data="inbox:triage:start"),
                InlineKeyboardButton(text=f"📥 Inbox ({int(inbox_count or 0)})", callback_data="nav:inbox:0"),
            ])
        elif inbox_id:
            kb.append([
                InlineKeyboardButton(text=f"📥 Inbox ({int(inbox_count or 0)})", callback_data="nav:inbox:0"),
            ])

        kb.append([
            InlineKeyboardButton(text=f"🔥 Срочно ({int(overdue_count or 0)}) →", callback_data="nav:overdue:0"),
            InlineKeyboardButton(text=f"⏰ Сегодня ({int(today_count or 0)}) →", callback_data="nav:today"),
        ])
        kb.append([
            InlineKeyboardButton(text=f"⚡ В работе ({int(work_count or 0)}) →", callback_data="nav:work:0"),
        ])
        kb.append([
            InlineKeyboardButton(text="📊 Статистика", callback_data="home:stats"),
            InlineKeyboardButton(text="🔄 Обновить", callback_data="nav:home"),
        ])

        await ui_render(
            bot=message.bot,
            db_pool=db_pool,
            chat_id=chat_id,
            text="\n".join(lines).strip(),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
            screen="home",
            fallback_message=message,
            parse_mode="HTML",
            force_new=force_new,
        )
    except Exception as e:
        try:
            await message.answer(f"❌ Ошибка: {e}")
        except Exception:
            return



async def ui_render_stats(message: Message | None, db_pool: asyncpg.Pool, *, tz_name: str | None = None, force_new: bool = False) -> None:
    """Render Stats dashboard (extended metrics)."""
    if not message:
        return
    chat_id = int(message.chat.id)
    tz_name = tz_name or _tz_name()
    tz = resolve_tzinfo(tz_name)

    try:
        async with db_pool.acquire() as conn:
            ui_state = await ui_get_state(conn, chat_id)
            payload = _ui_payload_get(ui_state)

            # Toast notification (one-shot)
            toast_line = None
            toast = payload.get("toast") if isinstance(payload, dict) else None
            if isinstance(toast, dict):
                exp = int(toast.get("exp") or 0)
                if not exp or exp >= _now_ts():
                    ttxt = (toast.get("text") or "").strip()
                    if ttxt:
                        toast_line = ttxt
                payload.pop("toast", None)

            current_project_code = await conn.fetchval(
                "SELECT p.code FROM projects p WHERE p.id = (SELECT current_project_id FROM user_settings WHERE chat_id=$1)",
                chat_id,
            )
            current_project_code = current_project_code or "—"

            inbox_id = await conn.fetchval("SELECT id FROM projects WHERE code='INBOX' LIMIT 1")

            overdue = await conn.fetchval(
                "SELECT COUNT(*) FROM tasks WHERE status != 'done' AND deadline IS NOT NULL AND deadline < (NOW() AT TIME ZONE 'UTC')"
            )

            if inbox_id:
                nodate = await conn.fetchval(
                    "SELECT COUNT(*) FROM tasks WHERE status != 'done' AND deadline IS NULL AND project_id != $1",
                    int(inbox_id),
                )
            else:
                nodate = await conn.fetchval(
                    "SELECT COUNT(*) FROM tasks WHERE status != 'done' AND deadline IS NULL"
                )

            today = await conn.fetchval(
                "SELECT COUNT(*) FROM tasks WHERE status != 'done' AND deadline IS NOT NULL "
                "AND (deadline AT TIME ZONE 'UTC' AT TIME ZONE $1)::date = (now() AT TIME ZONE $1)::date",
                tz_name,
            )

            inbox_count = 0
            if inbox_id:
                inbox_count = await conn.fetchval(
                    "SELECT COUNT(*) FROM tasks WHERE status != 'done' AND project_id=$1",
                    int(inbox_id),
                )

            projects = await conn.fetchval("SELECT COUNT(*) FROM projects")
            active_tasks = await conn.fetchval("SELECT COUNT(*) FROM tasks WHERE status != 'done'")

            next_rem = await conn.fetchval(
                "SELECT text FROM reminders WHERE is_sent=FALSE ORDER BY remind_at ASC LIMIT 1"
            )

            sync_row = await conn.fetchrow(
                "SELECT last_ok_at, last_error_at, last_error, last_duration_ms FROM sync_status WHERE name=$1",
                "vault",
            )

            # Save payload without toast
            await ui_set_state(conn, chat_id, ui_payload=payload)

        next_rem_txt = h(str(next_rem)) if next_rem else "—"

        # Sync status (Vault/Obsidian)
        sync_status_txt = "—"
        try:
            if sync_row:
                ok_at = sync_row["last_ok_at"]
                err_at = sync_row["last_error_at"]
                if ok_at and (not err_at or ok_at >= err_at):
                    sync_status_txt = f"✅ {fmt_local(ok_at, tz)}"
                elif err_at:
                    sync_status_txt = f"❌ {fmt_local(err_at, tz)}"
        except Exception:
            sync_status_txt = "—"

        lines = [
            "🧠 <b>Дашборд</b>",
            f"⭐ Текущий проект: <b>{h(str(current_project_code))}</b>",
            "",
            "<b>Внимание:</b>",
            f"🚨 Просрочено: <b>{int(overdue or 0)}</b>",
            f"🧺 Без срока (в работе): <b>{int(nodate or 0)}</b>",
            "",
            "<b>Фокус дня:</b>",
            f"📅 Задач на сегодня: <b>{int(today or 0)}</b>",
            f"🔔 Напомню: <i>{next_rem_txt}</i>",
            "",
            "<b>Интеграции:</b>",
            f"🔄 Obsidian: <i>{sync_status_txt}</i>",
            "",
            "<b>Пульс:</b>",
            f"📁 Проектов: <b>{int(projects or 0)}</b> | ✅ Задач: <b>{int(active_tasks or 0)}</b>",
            f"📥 Неразобрано (Inbox): <b>{int(inbox_count or 0)}</b>",
        ]

        if toast_line:
            lines.insert(0, toast_line)
            lines.insert(1, "")


        stats_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="⬅️ Фокус", callback_data="nav:home"),
                    InlineKeyboardButton(text="🔄 Обновить", callback_data="home:stats"),
                ],
                [
                    InlineKeyboardButton(text="⚡️ Быстрая задача", callback_data="quick:task"),
                    InlineKeyboardButton(text="➕ Добавить", callback_data="nav:add"),
                ],
                [InlineKeyboardButton(text="🔄 Синхронизация", callback_data="sync:status")],
            ]
        )

        await ui_render(
            bot=message.bot,
            db_pool=db_pool,
            chat_id=chat_id,
            text="\n".join(lines),
            reply_markup=stats_kb,
            screen="stats",
            fallback_message=message,
            parse_mode="HTML",
            force_new=force_new,
        )
    except Exception as e:
        try:
            await message.answer(f"❌ Ошибка: {e}")
        except Exception:
            return




async def ui_render_help(message: Message, db_pool: asyncpg.Pool, *, force_new: bool = False) -> None:
    help_text = (
        "❓ <b>Help</b>\n\n"
        "• Рабочий режим — один экран (сообщение) и кнопки под ним.\n"
        "• Кнопки внизу (ReplyKeyboard) можно нажимать — бот удалит твои сообщения и обновит экран.\n\n"
        "Разделы:\n"
        "📅 Сегодня — задачи на сегодня + напоминания.\n"
        "🚨 Просрочки — просроченные задачи и массовые действия.\n"
        "📁 Проекты — портфель → проект → задача → подзадачи.\n"
        "➕ Добавить — создание задач/событий/напоминаний.\n\n"
        "Подсказка: большинство действий не пишет «Ок», а просто обновляет экран."
    )
    await ui_render(
        bot=message.bot,
        db_pool=db_pool,
        chat_id=int(message.chat.id),
        text=help_text,
        reply_markup=back_home_kb(),
        screen="help",
        fallback_message=message,
        force_new=force_new,
        parse_mode="HTML",
    )


async def ui_render_add_menu(message: Message, db_pool: asyncpg.Pool, *, force_new: bool = False) -> None:
    await ui_render(
        bot=message.bot,
        db_pool=db_pool,
        chat_id=int(message.chat.id),
        text="➕ <b>Что добавить?</b>",
        reply_markup=add_menu_kb(),
        screen="add",
        fallback_message=message,
        force_new=force_new,
        parse_mode="HTML",
    )


async def ui_render_projects_portfolio(message: Message, db_pool: asyncpg.Pool, *, tz_name: str | None = None, force_new: bool = False) -> None:
    """Render active projects portfolio (Projects dashboard)."""
    chat_id = int(message.chat.id)
    try:
        async with db_pool.acquire() as conn:
            current_id = await conn.fetchval(
                "SELECT current_project_id FROM user_settings WHERE chat_id=$1",
                chat_id,
            )
            rows = await conn.fetch(
                '''
                SELECT p.id, p.code, p.name,
                       COUNT(t.id) FILTER (WHERE t.status != 'done') AS active_tasks,
                       COUNT(t.id) FILTER (WHERE t.status != 'done' AND t.deadline IS NOT NULL AND t.deadline < (NOW() AT TIME ZONE 'UTC')) AS overdue_tasks
                FROM projects p
                LEFT JOIN tasks t ON t.project_id = p.id
                WHERE p.status = 'active'
                GROUP BY p.id
                '''
            )

        rows = list(rows or [])
        if not rows:
            # Empty portfolio: still allow creating the first project
            empty_kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="➕ Новый проект", callback_data="proj:add:start"),
                        InlineKeyboardButton(text="➕ Задача", callback_data="add:task"),
                    ],
                    [InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home")],
                ]
            )
            return await ui_render(
                bot=message.bot,
                db_pool=db_pool,
                chat_id=chat_id,
                text="📭 <b>Активных проектов пока нет.</b>\n\nСоздайте первый проект 👇",
                reply_markup=empty_kb,
                screen="projects",
                payload={"mode": "portfolio"},
                fallback_message=message,
                parse_mode="HTML",
                force_new=force_new,
            )

        def sort_key(r):
            is_cur = (current_id is not None and int(r["id"]) == int(current_id))
            is_inbox = (r.get("code") == "INBOX")
            priority = 0 if is_cur else (1 if is_inbox else 2)
            return (priority, -int(r["overdue_tasks"] or 0), -int(r["active_tasks"] or 0), r["code"])

        rows_sorted = sorted(rows, key=sort_key)

        lines = ["<b>📁 ПРОЕКТЫ</b>", "<i>Портфель активных проектов</i>", ""]
        kb: list[list[InlineKeyboardButton]] = []
        proj_buttons_row: list[InlineKeyboardButton] = []

        for r in rows_sorted:
            code = r.get("code") or ""
            name = (r.get("name") or "").strip()
            active = int(r.get("active_tasks") or 0)
            overdue = int(r.get("overdue_tasks") or 0)
            is_cur = (current_id is not None and int(r["id"]) == int(current_id))

            title = f"<b>{h(code)}</b>" + (f" — {h(name)}" if name else "")
            meta_bits = [f"активных: {active}"]
            if overdue:
                meta_bits.append(f"🚨 {overdue}")
            if is_cur:
                meta_bits.append("⭐ текущий")
            meta = "<i>" + " • ".join(meta_bits) + "</i>"
            lines.append(title)
            lines.append(meta)

            btn_label = code
            if is_cur:
                btn_label = f"⭐ {btn_label}"
            elif overdue:
                btn_label = f"🚨{overdue} {btn_label}"

            proj_buttons_row.append(InlineKeyboardButton(text=btn_label, callback_data=f"proj:{r['id']}"))
            if len(proj_buttons_row) == 2:
                kb.append(proj_buttons_row)
                proj_buttons_row = []

        if proj_buttons_row:
            kb.append(proj_buttons_row)

        lines.append("")
        lines.append("Выберите проект ниже 👇")

        kb.append([
            InlineKeyboardButton(text="➕ Новый проект", callback_data="proj:add:start"),
            InlineKeyboardButton(text="➕ Задача", callback_data="add:task"),
        ])
        kb.append([InlineKeyboardButton(text="🧺 Глобальные хвосты", callback_data="nav:global_tails")])
        kb.append([InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home")])

        await ui_render(
            bot=message.bot,
            db_pool=db_pool,
            chat_id=chat_id,
            text="\n".join(lines).strip(),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
            screen="projects",
            payload={"mode": "portfolio"},
            fallback_message=message,
            parse_mode="HTML",
            force_new=force_new,
        )
    except Exception as e:
        await ui_render(
            bot=message.bot,
            db_pool=db_pool,
            chat_id=int(message.chat.id),
            text=f"❌ Ошибка: {h(str(e))}",
            reply_markup=back_home_kb(),
            screen="home",
            fallback_message=message,
            force_new=force_new,
            parse_mode="HTML",
        )


async def ui_render_team(message: Message, db_pool: asyncpg.Pool, *, force_new: bool = False) -> None:
    """Render Team dashboard (load by active tasks)."""
    chat_id = int(message.chat.id)
    tz = resolve_tzinfo(_tz_name())
    now_utc = datetime.now(UTC)
    today_local = datetime.now(tz).date()
    week_end_utc = now_utc + timedelta(days=7)

    try:
        async with db_pool.acquire() as conn:
            team_rows = await conn.fetch("SELECT id, name, role FROM team ORDER BY name")
            tasks_rows = await conn.fetch(
                "SELECT assignee_id, deadline FROM tasks WHERE status != 'done' AND assignee_id IS NOT NULL"
            )

        if not team_rows:
            return await ui_render(
                bot=message.bot,
                db_pool=db_pool,
                chat_id=chat_id,
                text="📭 <b>В команде пока никого нет.</b>",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="➕ Сотрудник", callback_data="team:add")],
                        [InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home")],
                    ]
                ),
                screen="team",
                fallback_message=message,
                force_new=force_new,
                parse_mode="HTML",
            )

        stats = {
            int(r["id"]): {"active": 0, "overdue": 0, "today": 0, "next7": 0, "nodate": 0}
            for r in team_rows
        }

        for t in tasks_rows:
            aid = t["assignee_id"]
            if aid is None:
                continue
            aid = int(aid)
            s = stats.get(aid)
            if not s:
                continue
            s["active"] += 1

            dl = t["deadline"]
            if not dl:
                s["nodate"] += 1
                continue

            dl_utc = to_utc(dl)
            if dl_utc and dl_utc < now_utc:
                s["overdue"] += 1

            if dl_utc:
                dl_local_date = dl_utc.astimezone(tz).date()
                if dl_local_date == today_local:
                    s["today"] += 1
                elif now_utc <= dl_utc <= week_end_utc:
                    s["next7"] += 1

        def sort_key(r):
            tid = int(r["id"])
            s = stats.get(tid, {"active": 0, "overdue": 0})
            return (-int(s.get("overdue", 0)), -int(s.get("active", 0)), str(r["name"] or ""))

        lines = ["<b>👥 Команда</b>", "<i>Загрузка по активным задачам</i>", ""]
        kb: list[list[InlineKeyboardButton]] = []
        member_buttons: list[InlineKeyboardButton] = []

        for r in sorted(team_rows, key=sort_key):
            tid = int(r["id"])
            s = stats.get(tid, {"active": 0, "overdue": 0, "today": 0, "next7": 0, "nodate": 0})
            lines.append(
                f"🔹 <b>{h(str(r['name'] or ''))}</b> — "
                f"активно: <b>{s['active']}</b> | 🚨 <b>{s['overdue']}</b> | "
                f"📅 {s['today']} | ⏳ {s['next7']} | 🧺 {s['nodate']}"
            )
            member_buttons.append(InlineKeyboardButton(text=str(r["name"]), callback_data=f"team:{tid}:0"))

        kb.extend(kb_columns(member_buttons, 2))
        kb.append([
            InlineKeyboardButton(text="➕ Сотрудник", callback_data="team:add"),
            InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home"),
        ])

        await ui_render(
            bot=message.bot,
            db_pool=db_pool,
            chat_id=chat_id,
            text="\n".join(lines).strip(),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
            screen="team",
            fallback_message=message,
            force_new=force_new,
            parse_mode="HTML",
        )
    except Exception as e:
        await ui_render(
            bot=message.bot,
            db_pool=db_pool,
            chat_id=chat_id,
            text=f"❌ Ошибка: {h(str(e))}",
            reply_markup=back_home_kb(),
            screen="team",
            fallback_message=message,
            force_new=force_new,
            parse_mode="HTML",
        )


async def ui_render_today(message: Message, db_pool: asyncpg.Pool, *, tz_name: str | None = None, force_new: bool = False) -> None:
    tz_name = tz_name or _tz_name()
    tz = resolve_tzinfo(tz_name)
    try:
        async with db_pool.acquire() as conn:
            tasks = await conn.fetch(
                """
                SELECT t.id, t.title, p.code as project, COALESCE(tm.name,'—') as assignee, t.deadline
                FROM tasks t
                JOIN projects p ON t.project_id = p.id
                LEFT JOIN team tm ON t.assignee_id = tm.id
                WHERE t.status NOT IN ('done', 'postponed')
                  AND t.deadline IS NOT NULL
                  AND (t.deadline AT TIME ZONE 'UTC' AT TIME ZONE $1)::date = (now() AT TIME ZONE $1)::date
                ORDER BY t.deadline ASC
                """,
                tz_name,
            )
            reminders = await conn.fetch(
                """
                SELECT id, text, remind_at
                FROM reminders
                WHERE is_sent = FALSE
                  AND (remind_at AT TIME ZONE 'UTC' AT TIME ZONE $1)::date = (now() AT TIME ZONE $1)::date
                ORDER BY remind_at ASC
                """,
                tz_name,
            )
        tasks = list(tasks or [])
        reminders = list(reminders or [])

        parts = ["<b>📅 ПЛАН НА СЕГОДНЯ</b>"]
        if not tasks and not reminders:
            parts.append("На сегодня нет задач и напоминаний 🎉")
            return await ui_render(
                bot=message.bot,
                db_pool=db_pool,
                chat_id=int(message.chat.id),
                text="\n".join(parts).strip(),
                reply_markup=today_screen_kb(False),
                screen="today",
                payload={"mode": "today"},
                fallback_message=message,
                parse_mode="HTML",
                force_new=force_new,
            )

        if tasks:
            parts.append("<b>📌 Задачи (дедлайн сегодня)</b>")
            for t in tasks:
                dt_local = to_local(t.get("deadline"), tz)
                parts.append("🔺 " + fmt_task_line_html(t.get("title") or "", t.get("project") or "", t.get("assignee") or "—", dt_local))

        if reminders:
            if tasks:
                parts.append("")
            parts.append("<b>⏰ Напоминания</b>")
            for r in reminders:
                dt_local = to_local(r.get("remind_at"), tz)
                hhmm = dt_local.strftime("%H:%M") if dt_local else "—"
                parts.append(f"🔔 <b>{h(hhmm)}</b> — {h(r.get('text') or '')}")

        await ui_render(
            bot=message.bot,
            db_pool=db_pool,
            chat_id=int(message.chat.id),
            text="\n".join(parts).strip(),
            reply_markup=today_screen_kb(bool(tasks)),
            screen="today",
            payload={"mode": "today"},
            fallback_message=message,
            parse_mode="HTML",
            force_new=force_new,
        )
    except Exception as e:
        await ui_render(
            bot=message.bot,
            db_pool=db_pool,
            chat_id=int(message.chat.id),
            text=f"❌ Ошибка: {h(str(e))}",
            reply_markup=back_home_kb(),
            screen="home",
            fallback_message=message,
            force_new=force_new,
            parse_mode="HTML",
        )




async def ui_render_work(message: Message, db_pool: asyncpg.Pool, *, tz_name: str | None = None, page: int = 0, force_new: bool = False) -> None:
    """Render tasks in status=in_progress (focus work list) with pagination."""
    tz_name = tz_name or _tz_name()
    tz = resolve_tzinfo(tz_name)
    chat_id = int(message.chat.id)

    page = max(0, int(page or 0))
    page_size = 20

    now_utc_naive = datetime.now(UTC).replace(tzinfo=None)

    def _short(s: str, n: int = 30) -> str:
        s = (s or "").strip()
        return s if len(s) <= n else (s[: n - 1] + "…")

    def _line(marker: str, project: str, title: str, assignee: str, dt_local: datetime | None) -> list[str]:
        proj = (project or "—").strip()
        t = (title or "").strip()
        a = (assignee or "—").strip()
        due = dt_local.strftime("%d.%m %H:%M") if dt_local else "без срока"
        if len(t) > 40:
            t_show = t if len(t) <= 90 else (t[:89] + "…")
            return [
                f"{marker} <b>[{h(proj)}]</b> {h(t_show)}",
                f"   {h(a)} → <i>{h('до ' + due) if dt_local else h(due)}</i>",
            ]
        t_show = t if len(t) <= 90 else (t[:89] + "…")
        due_part = f"до {due}" if dt_local else due
        return [f"{marker} <b>[{h(proj)}]</b> {h(t_show)} — {h(a)}, <i>{h(due_part)}</i>"]

    try:
        async with db_pool.acquire() as conn:
            current_project_id = await conn.fetchval(
                "SELECT current_project_id FROM user_settings WHERE chat_id=$1",
                chat_id,
            )
            current_project_code = None
            current_project_is_inbox = False
            if current_project_id:
                rowp = await conn.fetchrow("SELECT code FROM projects WHERE id=$1", int(current_project_id))
                if rowp and rowp.get("code"):
                    current_project_code = str(rowp["code"])
                    current_project_is_inbox = current_project_code.upper() == "INBOX"

            where = "t.status='in_progress'"
            args: list = []
            if current_project_id and not current_project_is_inbox:
                where += " AND t.project_id=$1"
                args = [int(current_project_id)]

            total = await conn.fetchval(f"SELECT COUNT(*) FROM tasks t WHERE {where}", *args) if args else await conn.fetchval(f"SELECT COUNT(*) FROM tasks t WHERE {where}")

            if args:
                rows = await conn.fetch(
                    f"""
                    SELECT t.id, t.title, p.code AS project, COALESCE(tm.name,'—') AS assignee, t.deadline
                    FROM tasks t
                    JOIN projects p ON p.id=t.project_id
                    LEFT JOIN team tm ON tm.id=t.assignee_id
                    WHERE {where}
                    ORDER BY t.deadline ASC NULLS LAST, t.created_at DESC
                    LIMIT $2 OFFSET $3
                    """,
                    *args,
                    page_size,
                    page * page_size,
                )
            else:
                rows = await conn.fetch(
                    f"""
                    SELECT t.id, t.title, p.code AS project, COALESCE(tm.name,'—') AS assignee, t.deadline
                    FROM tasks t
                    JOIN projects p ON p.id=t.project_id
                    LEFT JOIN team tm ON tm.id=t.assignee_id
                    WHERE {where}
                    ORDER BY t.deadline ASC NULLS LAST, t.created_at DESC
                    LIMIT $1 OFFSET $2
                    """,
                    page_size,
                    page * page_size,
                )
    except Exception as e:
        return await ui_render(
            bot=message.bot,
            db_pool=db_pool,
            chat_id=chat_id,
            text=f"❌ Ошибка: {h(str(e))}",
            reply_markup=back_home_kb(),
            screen="work",
            fallback_message=message,
            force_new=force_new,
            parse_mode="HTML",
        )

    rows = list(rows or [])
    total = int(total or 0)

    head = "<b>⚡ В РАБОТЕ</b>"
    if current_project_code and not current_project_is_inbox:
        head += f" — <b>{h(current_project_code)}</b>"
    lines = [head, f"<i>Всего: {total}</i>", ""]
    if not rows:
        lines.append("Задач в работе нет.")
    else:
        for r in rows:
            dl_local = to_local(r.get("deadline"), tz)
            for ln in _line("⚡", r.get("project") or "", r.get("title") or "", r.get("assignee") or "—", dl_local):
                lines.append(ln)

    # Keyboard
    kb: list[list[InlineKeyboardButton]] = []
    task_buttons: list[InlineKeyboardButton] = []
    for r in rows:
        proj = (r.get("project") or "").strip()
        title = (r.get("title") or "").strip()
        label = f"⚡{proj} {_short(title, 22)}" if proj else f"⚡{_short(title, 24)}"
        task_buttons.append(InlineKeyboardButton(text=label, callback_data=f"task:{r['id']}"))
    kb.extend(kb_columns(task_buttons, 2))

    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="⬅️", callback_data=f"nav:work:{page-1}"))
    if (page + 1) * page_size < total:
        nav_row.append(InlineKeyboardButton(text="➡️", callback_data=f"nav:work:{page+1}"))
    if nav_row:
        kb.append(nav_row)

    kb.append([
        InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home"),
        InlineKeyboardButton(text="📁 Проекты", callback_data="nav:projects"),
    ])

    await ui_render(
        bot=message.bot,
        db_pool=db_pool,
        chat_id=chat_id,
        text="\n".join(lines).strip(),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
        screen="work",
        payload={"page": page},
        fallback_message=message,
        force_new=force_new,
        parse_mode="HTML",
    )


async def ui_render_inbox(message: Message, db_pool: asyncpg.Pool, *, tz_name: str | None = None, page: int = 0, force_new: bool = False) -> None:
    """Render INBOX tasks list (GTD capture bucket) with pagination."""
    tz_name = tz_name or _tz_name()
    tz = resolve_tzinfo(tz_name)
    chat_id = int(message.chat.id)

    page = max(0, int(page or 0))
    page_size = 30

    def _short(s: str, n: int = 30) -> str:
        s = (s or "").strip()
        return s if len(s) <= n else (s[: n - 1] + "…")

    def _line(title: str, assignee: str, dt_local: datetime | None) -> list[str]:
        t = (title or "").strip()
        a = (assignee or "—").strip()
        due = dt_local.strftime("%d.%m %H:%M") if dt_local else "без срока"
        if len(t) > 40:
            t_show = t if len(t) <= 90 else (t[:89] + "…")
            return [
                f"📥 {h(t_show)}",
                f"   {h(a)} → <i>{h('до ' + due) if dt_local else h(due)}</i>",
            ]
        t_show = t if len(t) <= 90 else (t[:89] + "…")
        due_part = f"до {due}" if dt_local else due
        return [f"📥 {h(t_show)} — {h(a)}, <i>{h(due_part)}</i>"]

    toast_line: str | None = None

    try:
        async with db_pool.acquire() as conn:
            # Pull existing payload for one-shot toasts (like Home) without overwriting state.
            ui_state = await ui_get_state(conn, chat_id)
            payload = _ui_payload_get(ui_state)
            toast = payload.get("toast") if isinstance(payload, dict) else None
            if isinstance(toast, dict):
                exp = int(toast.get("exp") or 0)
                if not exp or exp >= _now_ts():
                    ttxt = (toast.get("text") or "").strip()
                    if ttxt:
                        toast_line = ttxt
                payload.pop("toast", None)

            # Remember current inbox page (non-critical)
            payload["inbox_page"] = int(page)
            await ui_set_state(conn, chat_id, ui_screen="inbox", ui_payload=payload)

            inbox_id = await conn.fetchval("SELECT id FROM projects WHERE code='INBOX' LIMIT 1")
            if not inbox_id:
                return await ui_render(
                    bot=message.bot,
                    db_pool=db_pool,
                    chat_id=chat_id,
                    text="❌ Проект INBOX не найден.",
                    reply_markup=back_home_kb(),
                    screen="inbox",
                    fallback_message=message,
                    force_new=force_new,
                    parse_mode="HTML",
                )

            total = await conn.fetchval(
                "SELECT COUNT(*) FROM tasks WHERE status != 'done' AND project_id=$1",
                int(inbox_id),
            )
            total = int(total or 0)

            rows = await conn.fetch(
                """
                SELECT t.id, t.title, COALESCE(tm.name,'—') AS assignee, t.deadline, t.created_at
                FROM tasks t
                LEFT JOIN team tm ON tm.id=t.assignee_id
                WHERE t.status != 'done' AND t.project_id=$1
                ORDER BY t.created_at ASC, t.id ASC
                LIMIT $2 OFFSET $3
                """,
                int(inbox_id),
                page_size,
                page * page_size,
            )
    except Exception as e:
        return await ui_render(
            bot=message.bot,
            db_pool=db_pool,
            chat_id=chat_id,
            text=f"❌ Ошибка: {h(str(e))}",
            reply_markup=back_home_kb(),
            screen="inbox",
            fallback_message=message,
            force_new=force_new,
            parse_mode="HTML",
        )

    rows = list(rows or [])

    lines: list[str] = []
    if toast_line:
        lines.extend([toast_line, ""])
    lines.extend(["📥 <b>INBOX</b> — входящие", f"<i>Всего: {total}</i>", ""])
    if not rows:
        lines.append("Inbox пуст. Добавляйте задачи через ➕ Добавить или ⚡ Быстрая задача.")
    else:
        lines.append("Нажмите <b>🧹 Разобрать</b>, чтобы пройтись по задачам по одной.")
        lines.append("")
        for r in rows:
            dl_local = to_local(r.get("deadline"), tz)
            lines.extend(_line(r.get("title") or "", r.get("assignee") or "—", dl_local))

    kb: list[list[InlineKeyboardButton]] = []
    kb.append(
        [
            InlineKeyboardButton(text="🧹 Разобрать", callback_data="inbox:triage:start"),
            InlineKeyboardButton(text="⚡️ Быстрая задача", callback_data="quick:task"),
        ]
    )

    # Task buttons (2 columns)
    task_buttons: list[InlineKeyboardButton] = []
    for r in rows:
        title = (r.get("title") or "").strip()
        label = f"📥 {_short(title, 26)}"
        task_buttons.append(InlineKeyboardButton(text=label, callback_data=f"task:{int(r['id'])}"))
    kb.extend(kb_columns(task_buttons, 2))

    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="⬅️", callback_data=f"nav:inbox:{page-1}"))
    if (page + 1) * page_size < total:
        nav_row.append(InlineKeyboardButton(text="➡️", callback_data=f"nav:inbox:{page+1}"))
    if nav_row:
        kb.append(nav_row)

    kb.append([
        InlineKeyboardButton(text="🔄 Обновить", callback_data=f"nav:inbox:{page}"),
        InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home"),
    ])

    await ui_render(
        bot=message.bot,
        db_pool=db_pool,
        chat_id=chat_id,
        text="\n".join(lines).strip(),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
        screen="inbox",
        fallback_message=message,
        force_new=force_new,
        parse_mode="HTML",
    )


async def ui_render_overdue(message: Message, db_pool: asyncpg.Pool, *, tz_name: str | None = None, page: int = 0, force_new: bool = False) -> None:
    """Render overdue tasks with pagination."""
    tz_name = tz_name or _tz_name()
    tz = resolve_tzinfo(tz_name)
    chat_id = int(message.chat.id)

    page = max(0, int(page or 0))
    page_size = 50
    now_utc_naive = datetime.now(UTC).replace(tzinfo=None)

    def _short(s: str, n: int = 30) -> str:
        s = (s or "").strip()
        return s if len(s) <= n else (s[: n - 1] + "…")

    def _task_lines(project: str, title: str, assignee: str, dt_local: datetime | None) -> list[str]:
        proj = (project or "—").strip()
        t = (title or "").strip()
        a = (assignee or "—").strip()
        due = dt_local.strftime("%d.%m %H:%M") if dt_local else "—"
        if len(t) > 40:
            t_show = t if len(t) <= 90 else (t[:89] + "…")
            return [
                f"🔥 <b>[{h(proj)}]</b> {h(t_show)}",
                f"   {h(a)} → <i>{h('был ' + due)}</i>",
            ]
        t_show = t if len(t) <= 90 else (t[:89] + "…")
        return [f"🔥 <b>[{h(proj)}]</b> {h(t_show)} — {h(a)}, <i>{h('был ' + due)}</i>"]

    try:
        async with db_pool.acquire() as conn:
            total = await conn.fetchval(
                "SELECT COUNT(*) FROM tasks WHERE status NOT IN ('done','postponed') AND deadline IS NOT NULL AND deadline < $1",
                now_utc_naive,
            )
            total = int(total or 0)
            rows = await conn.fetch(
                """
                SELECT t.id, t.title, p.code as project, COALESCE(tm.name,'—') as assignee, t.deadline
                FROM tasks t
                JOIN projects p ON t.project_id = p.id
                LEFT JOIN team tm ON t.assignee_id = tm.id
                WHERE t.status NOT IN ('done','postponed') AND t.deadline IS NOT NULL AND t.deadline < $1
                ORDER BY t.deadline ASC
                LIMIT $2 OFFSET $3
                """,
                now_utc_naive,
                page_size,
                page * page_size,
            )
    except Exception as e:
        return await ui_render(
            bot=message.bot,
            db_pool=db_pool,
            chat_id=chat_id,
            text=f"❌ Ошибка: {h(str(e))}",
            reply_markup=back_home_kb(),
            screen="overdue",
            fallback_message=message,
            force_new=force_new,
            parse_mode="HTML",
        )

    rows = list(rows or [])

    lines = [f"<b>🔥 СРОЧНО</b> — просроченные задачи", f"<i>Всего: {total}</i>", ""]
    kb: list[list[InlineKeyboardButton]] = []

    if not rows:
        lines = ["🎉 <b>Просроченных задач нет.</b>"]
    else:
        for r in rows:
            dt_local = to_local(r.get("deadline"), tz)
            lines.extend(_task_lines(r.get("project") or "", r.get("title") or "", r.get("assignee") or "—", dt_local))

        # Task buttons (2 columns)
        task_buttons: list[InlineKeyboardButton] = []
        for r in rows:
            proj = (r.get("project") or "").strip()
            title = (r.get("title") or "").strip()
            label = f"🔥{proj} {_short(title, 22)}" if proj else f"🔥{_short(title, 24)}"
            task_buttons.append(InlineKeyboardButton(text=label, callback_data=f"task:{r['id']}"))
        kb.extend(kb_columns(task_buttons, 2))

        # Bulk actions
        kb.append([InlineKeyboardButton(text="🧹 Разгрести", callback_data="bulk:start:0")])

        # Pagination
        nav_row: list[InlineKeyboardButton] = []
        if page > 0:
            nav_row.append(InlineKeyboardButton(text="⬅️", callback_data=f"nav:overdue:{page-1}"))
        if (page + 1) * page_size < total:
            nav_row.append(InlineKeyboardButton(text="➡️", callback_data=f"nav:overdue:{page+1}"))
        if nav_row:
            kb.append(nav_row)

    kb.append([
        InlineKeyboardButton(text="🔄 Обновить", callback_data=f"nav:overdue:{page}"),
        InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home"),
    ])

    await ui_render(
        bot=message.bot,
        db_pool=db_pool,
        chat_id=chat_id,
        text="\n".join(lines).strip(),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
        screen="overdue",
        payload={"page": page},
        fallback_message=message,
        force_new=force_new,
        parse_mode="HTML",
    )

