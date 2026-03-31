"""High-level SPA screens (renderers).

These functions render whole "screens" (Home/Projects/Today/Overdue/Help/Add),
updating the single UI message for the chat via :func:`bot.ui.render.ui_render`.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from bot.tz import resolve_tz_name, resolve_tzinfo

import asyncpg
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from bot.adapters.icloud_caldav_adapter import ICloudCalDAVAdapter, ICloudVisibleEvent
from bot.db.user_settings import get_persona_mode
from bot.persona import (
    is_solo_mode,
    persona_toggle_button_text,
    persona_toggle_target,
)
from bot.ui.render import ui_render
from bot.ui.state import ui_get_state, ui_set_state, _ui_payload_get, ui_payload_take_toast, ui_payload_with_toast
from bot.utils import h, fmt_task_line_html, kb_columns
from bot.keyboards import back_home_kb, add_menu_kb, main_menu_kb

logger = logging.getLogger(__name__)
_REMINDER_SELECTION_UNSET = object()


async def _take_screen_payload(conn: asyncpg.Connection, chat_id: int) -> tuple[dict, str | None]:
    ui_state = await ui_get_state(conn, chat_id)
    payload = _ui_payload_get(ui_state)
    toast_line, payload = ui_payload_take_toast(payload)
    return payload, toast_line


async def _pop_screen_toast(db_pool: asyncpg.Pool, chat_id: int) -> str | None:
    async with db_pool.acquire() as conn:
        payload, toast_line = await _take_screen_payload(conn, chat_id)
        await ui_set_state(conn, chat_id, ui_payload=payload)
    return toast_line


def _visible_assignee(assignee: str | None, persona_mode: str) -> str:
    if is_solo_mode(persona_mode):
        return ""
    value = (assignee or "—").strip()
    if not value or value == "—":
        return ""
    return value


async def _persona_mode_or_lead(db_pool: asyncpg.Pool | object, chat_id: int) -> str:
    try:
        acquire = getattr(db_pool, "acquire", None)
        if acquire is None:
            return "lead"
        async with db_pool.acquire() as conn:
            return await get_persona_mode(conn, chat_id)
    except Exception:
        return "lead"


async def ensure_main_menu(
    message: Message,
    db_pool: asyncpg.Pool,
    *,
    refresh: bool = False,
    recreate: bool = False,
) -> bool:
    """Ensure the persistent bottom reply-keyboard is visible.

    Telegram clients may hide reply keyboards; the most reliable way to restore
    it is to (re)send a message that carries the ReplyKeyboardMarkup. To avoid
    chat spam, we keep a single anchor message and leave it in place. By
    default this function is a cheap no-op when the anchor already exists.

    Returns ``True`` when a brand-new anchor message was sent. Callers can use
    that to force a fresh SPA render below the anchor when they want the layout
    to be "anchor first, SPA second".
    """

    chat_id = int(message.chat.id)

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT menu_message_id, persona_mode FROM user_settings WHERE chat_id=$1",
            chat_id,
        )
        menu_mid = row["menu_message_id"] if row else None
        persona_mode = "lead"
        if row:
            persona_mode = await get_persona_mode(conn, chat_id)

    # Keep anchor compact but explicit so recovery does not look like a blank phantom message.
    _ANCHOR_TEXT_A = "⌨️ Главное меню"
    _ANCHOR_TEXT_B = "⌨️ Меню активно"

    stale_menu_mid = int(menu_mid) if menu_mid else None
    if stale_menu_mid and not refresh and not recreate:
        return False

    if stale_menu_mid and not recreate:
        try:
            await message.bot.edit_message_text(
                chat_id=chat_id,
                message_id=stale_menu_mid,
                text=_ANCHOR_TEXT_A,
                reply_markup=main_menu_kb(persona_mode),
            )
            return False
        except TelegramBadRequest as e:
            if "message is not modified" in str(e).lower():
                try:
                    await message.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=stale_menu_mid,
                        text=_ANCHOR_TEXT_B,
                        reply_markup=main_menu_kb(persona_mode),
                    )
                    return False
                except TelegramBadRequest as retry_error:
                    if "message is not modified" in str(retry_error).lower():
                        return False
                except TelegramRetryAfter as retry_after:
                    await asyncio.sleep(float(getattr(retry_after, "retry_after", 1.0)) + 0.1)
        except TelegramRetryAfter as e:
            await asyncio.sleep(float(getattr(e, "retry_after", 1.0)) + 0.1)
        except Exception:
            pass

    try:
        anchor = await message.answer(_ANCHOR_TEXT_A, reply_markup=main_menu_kb(persona_mode))
        async with db_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO user_settings(chat_id, menu_message_id, updated_at) VALUES($1,$2,NOW()) "
                "ON CONFLICT(chat_id) DO UPDATE SET menu_message_id=EXCLUDED.menu_message_id, updated_at=NOW()",
                chat_id,
                int(anchor.message_id),
            )
        if stale_menu_mid and stale_menu_mid != int(anchor.message_id):
            try:
                await message.bot.delete_message(chat_id=chat_id, message_id=stale_menu_mid)
            except Exception:
                pass
        return True
    except Exception:
        return False


async def cleanup_main_menu_anchor(message: Message, db_pool: asyncpg.Pool) -> None:
    """Delete stored reply-keyboard anchor, if any.

    The keyboard itself is client-side state; removing the transport message is
    safe once the keyboard was already shown.
    """

    chat_id = int(message.chat.id)
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT menu_message_id FROM user_settings WHERE chat_id=$1",
            chat_id,
        )
        menu_mid = row["menu_message_id"] if row else None
        if not menu_mid:
            return
        await conn.execute(
            "UPDATE user_settings SET menu_message_id=NULL, updated_at=NOW() WHERE chat_id=$1",
            chat_id,
        )

    try:
        await message.bot.delete_message(chat_id=chat_id, message_id=int(menu_mid))
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


def _task_button_caption(
    *,
    title: str,
    project: str | None = None,
    deadline_local: datetime | None = None,
    status_hint: str | None = None,
    icon: str = "📝",
    max_title: int = 22,
) -> str:
    clean_title = (title or "").strip()
    short_title = clean_title if len(clean_title) <= max_title else (clean_title[: max_title - 1] + "…")
    parts = [icon]
    if project:
        parts.append(f"[{project}]")
    parts.append(short_title)
    meta = status_hint
    if meta is None and deadline_local is not None:
        meta = deadline_local.strftime("%d.%m %H:%M")
    if meta:
        parts.append(f"· {meta}")
    return " ".join(part for part in parts if part).strip()


def _single_column_task_buttons(
    rows: list,
    *,
    icon: str,
    title_key: str = "title",
    project_key: str | None = "project",
    deadline_key: str | None = "deadline",
    status_key: str | None = None,
    tz: ZoneInfo | None = None,
    quick_done: bool = False,
) -> list[list[InlineKeyboardButton]]:
    buttons: list[list[InlineKeyboardButton]] = []
    for row in rows:
        project = None
        if project_key:
            project = (row.get(project_key) or "").strip() or None
        deadline_local = None
        if deadline_key and tz is not None:
            deadline_local = to_local(row.get(deadline_key), tz)
        status_hint = None
        if status_key:
            raw_status = str(row.get(status_key) or "").strip().lower()
            if raw_status == "postponed":
                status_hint = "отложено"
            elif raw_status == "in_progress":
                status_hint = "в работе"
            elif raw_status == "done":
                status_hint = "готово"
        caption = _task_button_caption(
            title=str(row.get(title_key) or ""),
            project=project,
            deadline_local=deadline_local,
            status_hint=status_hint,
            icon=icon,
        )
        if quick_done:
            caption = f"✅ {caption}"
        callback_data = f"task:{int(row['id'])}:done_quick" if quick_done else f"task:{int(row['id'])}"
        buttons.append(
            [
                InlineKeyboardButton(
                    text=caption,
                    callback_data=callback_data,
                ),
            ]
        )
    return buttons


async def ui_render_home(
    message: Message | None,
    db_pool: asyncpg.Pool,
    *,
    tz_name: str | None = None,
    preferred_message_id: int | None = None,
    force_new: bool = False,
) -> int:
    """Render GTD Focus Home screen (lightweight)."""
    if not message:
        return 0

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

    def _preview_lines(
        marker: str,
        project: str,
        title: str,
        assignee: str,
        dt_local: datetime | None,
        mode: str,
        *,
        persona_mode: str,
    ) -> list[str]:
        proj = (project or "—").strip()
        t = (title or "").strip()
        a = _visible_assignee(assignee, persona_mode)
        # keep lines readable
        t_show = t if len(t) <= 90 else (t[:89] + "…")
        due = _due_str(dt_local, mode)
        if len(t) > 40:
            if a:
                return [
                    f"{marker} <b>[{h(proj)}]</b> {h(t_show)}",
                    f"   {h(a)} → <i>{h(due)}</i>",
                ]
            return [
                f"{marker} <b>[{h(proj)}]</b> {h(t_show)}",
                f"   <i>{h(due)}</i>",
            ]
        if a:
            return [f"{marker} <b>[{h(proj)}]</b> {h(t_show)} — {h(a)}, <i>{h(due)}</i>"]
        return [f"{marker} <b>[{h(proj)}]</b> {h(t_show)} — <i>{h(due)}</i>"]

    try:
        async with db_pool.acquire() as conn:
            payload, toast_line = await _take_screen_payload(conn, chat_id)
            persona_mode = await get_persona_mode(conn, chat_id)

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
                "SELECT COUNT(*) FROM tasks WHERE status NOT IN ('done','postponed') AND kind != 'super' AND deadline IS NOT NULL AND deadline < $1",
                now_utc_naive,
            )
            today_count = await conn.fetchval(
                "SELECT COUNT(*) FROM tasks WHERE status NOT IN ('done','postponed') AND kind != 'super' AND deadline IS NOT NULL AND deadline >= $1 AND deadline < $2",
                start_utc_naive,
                end_utc_naive,
            )
            inbox_count = 0
            if inbox_id:
                inbox_count = await conn.fetchval(
                    "SELECT COUNT(*) FROM tasks WHERE status != 'done' AND kind != 'super' AND project_id=$1",
                    int(inbox_id),
                )

            # Work (in_progress): focus within current project if set and not INBOX; otherwise global.
            work_where = "t.status='in_progress' AND t.kind != 'super'"
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
                WHERE t.status NOT IN ('done','postponed') AND t.kind != 'super' AND t.deadline IS NOT NULL AND t.deadline < $1
                ORDER BY t.deadline ASC
                LIMIT 1
                """,
                now_utc_naive,
            )

            today_rows = await conn.fetch(
                """
                SELECT t.id, t.title, p.code AS project, COALESCE(tm.name,'—') AS assignee, t.deadline
                FROM tasks t
                JOIN projects p ON p.id=t.project_id
                LEFT JOIN team tm ON tm.id=t.assignee_id
                WHERE t.status NOT IN ('done','postponed') AND t.kind != 'super' AND t.deadline IS NOT NULL AND t.deadline >= $1 AND t.deadline < $2
                ORDER BY t.deadline ASC
                LIMIT 1
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
                    LIMIT 1
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
                    LIMIT 1
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
        lines.append(
            f"🔥 Срочно: <b>{int(overdue_count or 0)}</b> • "
            f"⏰ Сегодня: <b>{int(today_count or 0)}</b> • "
            f"⚡ В работе: <b>{int(work_count or 0)}</b> • "
            f"📥 Inbox: <b>{int(inbox_count or 0)}</b>"
        )

        lines.append("")
        lines.append("<b>Ближайшее</b>")
        if overdue_rows:
            r = overdue_rows[0]
            dt_local = to_local(r.get("deadline"), tz)
            lines.extend(
                _preview_lines(
                    "🔥",
                    r.get("project") or "",
                    r.get("title") or "",
                    r.get("assignee") or "—",
                    dt_local,
                    "overdue",
                    persona_mode=persona_mode,
                )
            )
        if today_rows:
            r = today_rows[0]
            dt_local = to_local(r.get("deadline"), tz)
            lines.extend(
                _preview_lines(
                    "⏰",
                    r.get("project") or "",
                    r.get("title") or "",
                    r.get("assignee") or "—",
                    dt_local,
                    "today",
                    persona_mode=persona_mode,
                )
            )
        if work_rows:
            r = work_rows[0]
            dt_local = to_local(r.get("deadline"), tz)
            lines.extend(
                _preview_lines(
                    "⚡",
                    r.get("project") or "",
                    r.get("title") or "",
                    r.get("assignee") or "—",
                    dt_local,
                    "work",
                    persona_mode=persona_mode,
                )
            )
        if not (overdue_rows or today_rows or work_rows):
            lines.append("—")

        kb: list[list[InlineKeyboardButton]] = [
            [
                InlineKeyboardButton(text="⚡ Быстрая задача", callback_data="quick:task"),
                InlineKeyboardButton(text="💡 Идея", callback_data="quick:idea"),
            ]
        ]
        if inbox_id:
            if int(inbox_count or 0) > 0:
                kb.append([
                    InlineKeyboardButton(text=f"📥 Inbox ({int(inbox_count or 0)})", callback_data="nav:inbox:0"),
                    InlineKeyboardButton(text="🧹 Разобрать Inbox", callback_data="inbox:triage:start"),
                ])
            else:
                kb.append([
                    InlineKeyboardButton(text=f"📥 Inbox ({int(inbox_count or 0)})", callback_data="nav:inbox:0"),
                    InlineKeyboardButton(text=f"⚡ В работе ({int(work_count or 0)})", callback_data="nav:work:0"),
                ])
        else:
            kb.append([InlineKeyboardButton(text=f"⚡ В работе ({int(work_count or 0)})", callback_data="nav:work:0")])
        kb.append([InlineKeyboardButton(text="⋯ Ещё", callback_data="nav:secondary")])

        return await ui_render(
            bot=message.bot,
            db_pool=db_pool,
            chat_id=chat_id,
            text="\n".join(lines).strip(),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
            screen="home",
            fallback_message=message,
            preferred_message_id=preferred_message_id,
            parse_mode="HTML",
            force_new=force_new,
        )
    except Exception:
        logger.exception("ui_render_home failed", extra={"chat_id": chat_id})
        fallback_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="🔄 Обновить", callback_data="nav:home"),
                    InlineKeyboardButton(text="📁 Проекты", callback_data="nav:projects"),
                ],
                [InlineKeyboardButton(text="❓ Помощь", callback_data="nav:help")],
            ]
        )
        return await ui_render(
            bot=message.bot,
            db_pool=db_pool,
            chat_id=chat_id,
            text="⚠️ <b>Не удалось обновить главный экран.</b>\n\nПопробуйте ещё раз.",
            reply_markup=fallback_kb,
            screen="home",
            fallback_message=message,
            preferred_message_id=preferred_message_id,
            parse_mode="HTML",
            force_new=force_new,
        )



async def ui_render_stats(
    message: Message | None,
    db_pool: asyncpg.Pool,
    *,
    tz_name: str | None = None,
    preferred_message_id: int | None = None,
    force_new: bool = False,
) -> int:
    """Render Stats dashboard (extended metrics)."""
    if not message:
        return 0
    chat_id = int(message.chat.id)
    tz_name = tz_name or _tz_name()
    tz = resolve_tzinfo(tz_name)

    try:
        async with db_pool.acquire() as conn:
            payload, toast_line = await _take_screen_payload(conn, chat_id)

            current_project_code = await conn.fetchval(
                "SELECT p.code FROM projects p WHERE p.id = (SELECT current_project_id FROM user_settings WHERE chat_id=$1)",
                chat_id,
            )
            current_project_code = current_project_code or "—"

            inbox_id = await conn.fetchval("SELECT id FROM projects WHERE code='INBOX' LIMIT 1")
            overdue = await conn.fetchval(
                "SELECT COUNT(*) FROM tasks WHERE status != 'done' AND kind != 'super' AND deadline IS NOT NULL AND deadline < (NOW() AT TIME ZONE 'UTC')"
            )
            if inbox_id:
                nodate = await conn.fetchval(
                    "SELECT COUNT(*) FROM tasks WHERE status != 'done' AND kind != 'super' AND deadline IS NULL AND project_id != $1",
                    int(inbox_id),
                )
            else:
                nodate = await conn.fetchval(
                    "SELECT COUNT(*) FROM tasks WHERE status != 'done' AND kind != 'super' AND deadline IS NULL"
                )

            today = await conn.fetchval(
                "SELECT COUNT(*) FROM tasks WHERE status != 'done' AND kind != 'super' AND deadline IS NOT NULL "
                "AND (deadline AT TIME ZONE 'UTC' AT TIME ZONE $1)::date = (now() AT TIME ZONE $1)::date",
                tz_name,
            )

            inbox_count = 0
            if inbox_id:
                inbox_count = await conn.fetchval(
                    "SELECT COUNT(*) FROM tasks WHERE status != 'done' AND kind != 'super' AND project_id=$1",
                    int(inbox_id),
                )

            projects = await conn.fetchval("SELECT COUNT(*) FROM projects")
            active_tasks = await conn.fetchval("SELECT COUNT(*) FROM tasks WHERE status != 'done' AND kind != 'super'")
            next_rem = await conn.fetchval(
                "SELECT text FROM reminders WHERE is_sent=FALSE ORDER BY remind_at ASC LIMIT 1"
            )
            sync_row = await conn.fetchrow(
                "SELECT last_ok_at, last_error_at, last_error, last_duration_ms FROM sync_status WHERE name=$1",
                "vault",
            )
            await ui_set_state(conn, chat_id, ui_payload=payload)

        next_rem_txt = h(str(next_rem)) if next_rem else "—"
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
            "🧠 <b>Статистика</b>",
            f"<i>Текущий проект: {h(str(current_project_code))}</i>",
            "",
            f"🚨 Просрочено: <b>{int(overdue or 0)}</b>",
            f"📅 Сегодня: <b>{int(today or 0)}</b>",
            f"📥 Inbox: <b>{int(inbox_count or 0)}</b>",
            f"🧺 Без срока: <b>{int(nodate or 0)}</b>",
            "",
            f"📁 Проектов: <b>{int(projects or 0)}</b> • ✅ Активных задач: <b>{int(active_tasks or 0)}</b>",
            f"🔔 Ближайшее напоминание: <i>{next_rem_txt}</i>",
            f"🔄 Obsidian: <i>{sync_status_txt}</i>",
        ]
        if toast_line:
            lines.insert(0, toast_line)
            lines.insert(1, "")

        stats_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="🔄 Обновить", callback_data="home:stats"),
                    InlineKeyboardButton(text="🔄 Синхронизация", callback_data="sync:status"),
                ],
                [
                    InlineKeyboardButton(text="⋯ Ещё", callback_data="nav:secondary"),
                    InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home"),
                ],
            ]
        )

        return await ui_render(
            bot=message.bot,
            db_pool=db_pool,
            chat_id=chat_id,
            text="\n".join(lines),
            reply_markup=stats_kb,
            screen="stats",
            fallback_message=message,
            preferred_message_id=preferred_message_id,
            parse_mode="HTML",
            force_new=force_new,
        )
    except Exception:
        logger.exception("ui_render_stats failed", extra={"chat_id": chat_id})
        fallback_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="🔄 Обновить", callback_data="home:stats"),
                    InlineKeyboardButton(text="⋯ Ещё", callback_data="nav:secondary"),
                ],
                [InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home")],
            ]
        )
        return await ui_render(
            bot=message.bot,
            db_pool=db_pool,
            chat_id=chat_id,
            text="⚠️ <b>Не удалось обновить статистику.</b>\n\nПопробуйте ещё раз.",
            reply_markup=fallback_kb,
            screen="stats",
            fallback_message=message,
            preferred_message_id=preferred_message_id,
            parse_mode="HTML",
            force_new=force_new,
        )

async def ui_render_home_more(
    message: Message,
    db_pool: asyncpg.Pool,
    *,
    preferred_message_id: int | None = None,
    force_new: bool = False,
) -> int:
    chat_id = int(message.chat.id)
    toast_line = await _pop_screen_toast(db_pool, chat_id)
    persona_mode = await _persona_mode_or_lead(db_pool, chat_id)

    lines = ["⋯ <b>Ещё</b>", "", "Вспомогательные разделы и редкие действия."]
    if toast_line:
        lines = [toast_line, ""] + lines

    kb_rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text="📊 Статистика", callback_data="home:stats"),
            InlineKeyboardButton(text="🔄 Синхронизация", callback_data="sync:status"),
        ],
    ]
    if is_solo_mode(persona_mode):
        kb_rows.append(
            [
                InlineKeyboardButton(text="❓ Помощь", callback_data="nav:help"),
                InlineKeyboardButton(
                    text=persona_toggle_button_text(persona_mode),
                    callback_data=f"settings:persona:{persona_toggle_target(persona_mode)}",
                ),
            ]
        )
    else:
        kb_rows.append(
            [
                InlineKeyboardButton(text="❓ Помощь", callback_data="nav:help"),
                InlineKeyboardButton(text="👥 Команда", callback_data="nav:team"),
            ]
        )
        kb_rows.append(
            [
                InlineKeyboardButton(
                    text=persona_toggle_button_text(persona_mode),
                    callback_data=f"settings:persona:{persona_toggle_target(persona_mode)}",
                )
            ]
        )
    kb_rows.append([InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home")])

    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

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
    chat_id = int(message.chat.id)
    toast_line = await _pop_screen_toast(db_pool, chat_id)
    persona_mode = await _persona_mode_or_lead(db_pool, chat_id)
    help_lines = [
        "❓ <b>Короткая справка</b>",
        "",
        "• Нижнее меню дает быстрый вход в ежедневные разделы.",
        "• Большинство действий обновляет один экран, без лишней ленты.",
        "• Для задачи чаще всего достаточно открыть карточку и выбрать `✅` или `📅`.",
        "• Быстрое создание работает через `⚡ Быстрая задача` или `➕ Добавить`.",
        "",
        "<b>Основные разделы</b>",
        "• 📅 Сегодня: план дня, напоминания и календарные события.",
        "• 📋 Все задачи: общий список с фильтрами, включая просрочку.",
        "• 📁 Проекты: структура и рабочие списки.",
        "• 🔔 Напоминания: активные напоминания и snooze.",
        "• ➕ Добавить: создать задачу, событие или напоминание.",
    ]
    if is_solo_mode(persona_mode):
        help_lines.append("• ⚡ В работе: быстрый вход в активные задачи.")
    else:
        help_lines.append("• 👥 Команда: сотрудники и их загрузка.")
    help_lines.extend(
        [
            "• /help: открыть эту справку в любой момент.",
            "",
            "<b>Пример свободного ввода</b>",
            "<i>напомни купить хлеб завтра в 18:00</i>",
        ]
    )
    help_text = "\n".join(help_lines)
    if toast_line:
        help_text = f"{toast_line}\n\n{help_text}"
    return await ui_render(
        bot=message.bot,
        db_pool=db_pool,
        chat_id=int(message.chat.id),
        text=help_text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="📅 Сегодня", callback_data="nav:today"),
                    InlineKeyboardButton(text="➕ Добавить", callback_data="nav:add"),
                ],
                [
                    InlineKeyboardButton(text="⋯ Ещё", callback_data="nav:secondary"),
                    InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home"),
                ],
            ]
        ),
        screen="help",
        fallback_message=message,
        preferred_message_id=preferred_message_id,
        force_new=force_new,
        parse_mode="HTML",
    )


async def ui_render_reminders(
    message: Message,
    db_pool: asyncpg.Pool,
    *,
    tz_name: str | None = None,
    page: int | None = None,
    selected_reminder_id: int | None | object = _REMINDER_SELECTION_UNSET,
    preferred_message_id: int | None = None,
    force_new: bool = False,
) -> int:
    """Render the active reminders management screen."""
    chat_id = int(message.chat.id)
    tz_name = tz_name or _tz_name()
    tz = resolve_tzinfo(tz_name)
    page_size = 8

    try:
        async with db_pool.acquire() as conn:
            payload, toast_line = await _take_screen_payload(conn, chat_id)
            stored_page = max(0, int(payload.get("reminders_page") or 0))
            if page is None:
                page = stored_page

            total = await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM reminders
                WHERE chat_id=$1
                  AND cancelled_at_utc IS NULL
                  AND status IN ('pending', 'retry', 'claimed')
                """,
                chat_id,
            )
            total = int(total or 0)
            max_page = max(0, ((total - 1) // page_size) if total else 0)
            page = min(max(0, int(page or 0)), max_page)

            rows = await conn.fetch(
                """
                SELECT id, text, remind_at, next_attempt_at_utc, repeat
                FROM reminders
                WHERE chat_id=$1
                  AND cancelled_at_utc IS NULL
                  AND status IN ('pending', 'retry', 'claimed')
                ORDER BY COALESCE(next_attempt_at_utc, remind_at AT TIME ZONE 'UTC') ASC
                LIMIT $2 OFFSET $3
                """,
                chat_id,
                page_size,
                int(page or 0) * page_size,
            )
    except Exception:
        logger.exception("ui_render_reminders db failed", extra={"chat_id": chat_id})
        payload = {}
        toast_line = None
        total = 0
        max_page = 0
        page = 0
        rows = []

    if selected_reminder_id is _REMINDER_SELECTION_UNSET:
        selected = int(payload.get("selected_reminder_id") or 0) or None
    else:
        selected = int(selected_reminder_id or 0) or None
    row_ids = {int(r["id"]) for r in rows}
    if selected not in row_ids:
        selected = None

    lines: list[str] = []
    if toast_line:
        lines.extend([toast_line, ""])

    lines.append("🔔 <b>Активные напоминания</b>")
    lines.append(f"<i>Всего: {total}</i>")
    if total > page_size:
        lines.append("")
        lines.append(f"<i>Страница {int(page or 0) + 1} из {max_page + 1}.</i>")

    kb: list[list[InlineKeyboardButton]] = []
    kb.append([
        InlineKeyboardButton(text="➕ Напоминание", callback_data="add:rem"),
        InlineKeyboardButton(text="🔄 Обновить", callback_data=f"nav:reminders:{int(page or 0)}"),
    ])

    if not rows:
        lines.append("")
        lines.append("Нет активных напоминаний.")
    else:
        lines.append("")
        if selected is None:
            lines.append("<i>Нажмите на напоминание ниже, чтобы открыть действия.</i>")
        else:
            lines.append("<i>Действия для выбранного напоминания показаны под ним.</i>")
        _repeat_labels = {
            "daily": "ежедн.",
            "weekly": "еженед.",
            "workdays": "будни",
            "monthly": "ежемес.",
        }
        from datetime import timezone
        for r in rows:
            rid = int(r["id"])
            raw_dt = r.get("next_attempt_at_utc") or r.get("remind_at")
            if raw_dt is not None:
                dt_utc = raw_dt if raw_dt.tzinfo else raw_dt.replace(tzinfo=timezone.utc)
                dt_local = dt_utc.astimezone(tz)
                when = dt_local.strftime("%d.%m %H:%M")
            else:
                when = "?"
            rep = (r.get("repeat") or "none").lower()
            rep_label = _repeat_labels.get(rep, "")
            rep_str = f" [{rep_label}]" if rep_label else ""
            text_raw = (r.get("text") or "").strip()
            btn_text = f"{when} · {text_raw}" if text_raw else when
            btn_text = btn_text if len(btn_text) <= 36 else btn_text[:35] + "…"
            if rep_str:
                btn_text = f"{btn_text}{rep_str}"
            if selected == rid:
                btn_text = f"▶ {btn_text}"
            kb.append([
                InlineKeyboardButton(text=btn_text, callback_data=f"rem:pick:{int(page or 0)}:{rid}")
            ])
            if selected == rid:
                kb.append([
                    InlineKeyboardButton(text="📝 В задачу", callback_data=f"rem:task:{rid}"),
                    InlineKeyboardButton(text="🗑 Удалить…", callback_data=f"rem:cancel_ask:{rid}:{int(page or 0)}")
                ])
                kb.append([
                    InlineKeyboardButton(text="⏸ 15м", callback_data=f"rem:snooze:15:{rid}:{int(page or 0)}"),
                    InlineKeyboardButton(text="⏸ 3ч", callback_data=f"rem:snooze:180:{rid}:{int(page or 0)}"),
                    InlineKeyboardButton(text="⏳ Завтра", callback_data=f"rem:snooze:tom:{rid}:{int(page or 0)}"),
                ])

    if max_page > 0:
        nav_row: list[InlineKeyboardButton] = []
        if int(page or 0) > 0:
            nav_row.append(InlineKeyboardButton(text="⬅️", callback_data=f"nav:reminders:{int(page or 0) - 1}"))
        nav_row.append(InlineKeyboardButton(text=f"{int(page or 0) + 1}/{max_page + 1}", callback_data=f"nav:reminders:{int(page or 0)}"))
        if int(page or 0) < max_page:
            nav_row.append(InlineKeyboardButton(text="➡️", callback_data=f"nav:reminders:{int(page or 0) + 1}"))
        kb.append(nav_row)

    kb.append([InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home")])
    payload["reminders_page"] = int(page or 0)
    if selected is None:
        payload.pop("selected_reminder_id", None)
    else:
        payload["selected_reminder_id"] = int(selected)

    return await ui_render(
        bot=message.bot,
        db_pool=db_pool,
        chat_id=chat_id,
        text="\n".join(lines).strip(),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
        screen="reminders",
        payload=payload,
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
    toast_line = await _pop_screen_toast(db_pool, int(message.chat.id))
    text = "➕ <b>Что добавить?</b>"
    if toast_line:
        text = f"{toast_line}\n\n{text}"
    return await ui_render(
        bot=message.bot,
        db_pool=db_pool,
        chat_id=int(message.chat.id),
        text=text,
        reply_markup=add_menu_kb(),
        screen="add",
        fallback_message=message,
        preferred_message_id=preferred_message_id,
        force_new=force_new,
        parse_mode="HTML",
    )


async def ui_render_projects_portfolio(
    message: Message,
    db_pool: asyncpg.Pool,
    *,
    tz_name: str | None = None,
    preferred_message_id: int | None = None,
    force_new: bool = False,
) -> int:
    """Render active projects portfolio (Projects dashboard)."""
    chat_id = int(message.chat.id)
    toast_line = await _pop_screen_toast(db_pool, chat_id)
    try:
        async with db_pool.acquire() as conn:
            current_id = await conn.fetchval(
                "SELECT current_project_id FROM user_settings WHERE chat_id=$1",
                chat_id,
            )
            rows = await conn.fetch(
                '''
                SELECT p.id, p.code, p.name,
                       COUNT(t.id) FILTER (WHERE t.status != 'done' AND t.kind != 'super') AS active_tasks,
                       COUNT(t.id) FILTER (WHERE t.status != 'done' AND t.kind != 'super' AND t.deadline IS NOT NULL AND t.deadline < (NOW() AT TIME ZONE 'UTC')) AS overdue_tasks
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
                        InlineKeyboardButton(text="⚡️ Быстрая задача", callback_data="quick:task"),
                    ],
                    [InlineKeyboardButton(text="📋 Все задачи", callback_data="nav:all")],
                    [InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home")],
                ]
            )
            text = "📭 <b>Активных проектов пока нет.</b>\n\nСоздайте первый проект 👇"
            if toast_line:
                text = f"{toast_line}\n\n{text}"
            return await ui_render(
                bot=message.bot,
                db_pool=db_pool,
                chat_id=chat_id,
                text=text,
                reply_markup=empty_kb,
                screen="projects",
                payload={"mode": "portfolio"},
                fallback_message=message,
                preferred_message_id=preferred_message_id,
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
        if toast_line:
            lines = [toast_line, ""] + lines
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
        kb.append([
            InlineKeyboardButton(text="📋 Все задачи", callback_data="nav:all"),
            InlineKeyboardButton(text="🧺 Хвосты", callback_data="nav:global_tails"),
        ])
        kb.append([InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home")])

        return await ui_render(
            bot=message.bot,
            db_pool=db_pool,
            chat_id=chat_id,
            text="\n".join(lines).strip(),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
            screen="projects",
            payload={"mode": "portfolio"},
            fallback_message=message,
            preferred_message_id=preferred_message_id,
            parse_mode="HTML",
            force_new=force_new,
        )
    except Exception as e:
        return await ui_render(
            bot=message.bot,
            db_pool=db_pool,
            chat_id=int(message.chat.id),
            text=f"❌ Ошибка загрузки. Для фикса: {h(str(e))}",
            reply_markup=back_home_kb(),
            screen="home",
            fallback_message=message,
            preferred_message_id=preferred_message_id,
            force_new=force_new,
            parse_mode="HTML",
        )


async def ui_render_team(
    message: Message,
    db_pool: asyncpg.Pool,
    *,
    preferred_message_id: int | None = None,
    force_new: bool = False,
) -> int:
    """Render Team dashboard (load by active tasks)."""
    chat_id = int(message.chat.id)
    redirect_solo = False
    async with db_pool.acquire() as conn:
        persona_mode = await get_persona_mode(conn, chat_id)
        if is_solo_mode(persona_mode):
            ui_state = await ui_get_state(conn, chat_id)
            payload = ui_payload_with_toast(
                _ui_payload_get(ui_state),
                "👤 Режим Solo скрывает раздел Команда. Переключите режим через ⋯ Ещё.",
                ttl_sec=25,
            )
            await ui_set_state(conn, chat_id, ui_payload=payload)
            redirect_solo = True
    if redirect_solo:
        return await ui_render_home_more(
            message,
            db_pool,
            preferred_message_id=preferred_message_id,
            force_new=force_new,
        )
    toast_line = await _pop_screen_toast(db_pool, chat_id)
    tz = resolve_tzinfo(_tz_name())
    now_utc = datetime.now(UTC)
    today_local = datetime.now(tz).date()
    week_end_utc = now_utc + timedelta(days=7)

    try:
        async with db_pool.acquire() as conn:
            team_rows = await conn.fetch("SELECT id, name, role FROM team ORDER BY name")
            tasks_rows = await conn.fetch(
                "SELECT assignee_id, deadline FROM tasks WHERE status != 'done' AND kind != 'super' AND assignee_id IS NOT NULL"
            )

        if not team_rows:
            text = "📭 <b>В команде пока никого нет.</b>"
            if toast_line:
                text = f"{toast_line}\n\n{text}"
            return await ui_render(
                bot=message.bot,
                db_pool=db_pool,
                chat_id=chat_id,
                text=text,
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="➕ Сотрудник", callback_data="team:add")],
                        [InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home")],
                    ]
                ),
                screen="team",
                fallback_message=message,
                preferred_message_id=preferred_message_id,
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

        total_members = len(team_rows)
        total_active = sum(int(s["active"]) for s in stats.values())
        total_overdue = sum(int(s["overdue"]) for s in stats.values())
        lines = [
            "<b>👥 Команда</b>",
            f"<i>Участников: {total_members} • Активных задач: {total_active} • Просрочено: {total_overdue}</i>",
            "",
        ]
        if toast_line:
            lines = [toast_line, ""] + lines
        kb: list[list[InlineKeyboardButton]] = []
        member_buttons: list[InlineKeyboardButton] = []

        def _team_button_text(name: str, active: int) -> str:
            clean_name = str(name or "").strip() or "Без имени"
            max_name = 11
            if len(clean_name) > max_name:
                clean_name = clean_name[: max_name - 1] + "…"
            return f"👤 {clean_name} · {int(active)}"

        for r in sorted(team_rows, key=sort_key):
            tid = int(r["id"])
            s = stats.get(tid, {"active": 0, "overdue": 0, "today": 0, "next7": 0, "nodate": 0})
            name = str(r["name"] or "")
            member_buttons.append(
                InlineKeyboardButton(
                    text=_team_button_text(name, s["active"]),
                    callback_data=f"team:{tid}:0",
                )
            )

        kb.extend(kb_columns(member_buttons, 2))

        kb.append([InlineKeyboardButton(text="➕ Сотрудник", callback_data="team:add")])
        kb.append([InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home")])

        return await ui_render(
            bot=message.bot,
            db_pool=db_pool,
            chat_id=chat_id,
            text="\n".join(lines).strip(),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
            screen="team",
            fallback_message=message,
            preferred_message_id=preferred_message_id,
            force_new=force_new,
            parse_mode="HTML",
        )
    except Exception as e:
        return await ui_render(
            bot=message.bot,
            db_pool=db_pool,
            chat_id=chat_id,
            text=f"❌ Ошибка загрузки. Для фикса: {h(str(e))}",
            reply_markup=back_home_kb(),
            screen="team",
            fallback_message=message,
            preferred_message_id=preferred_message_id,
            force_new=force_new,
            parse_mode="HTML",
        )


def _event_calendar_icon(calendar_url: str, *, work_calendar_url: str, personal_calendar_url: str) -> str:
    calendar_url = (calendar_url or "").strip()
    if work_calendar_url and calendar_url == work_calendar_url:
        return "💼"
    if personal_calendar_url and calendar_url == personal_calendar_url:
        return "🏡"
    return "📅"


def _event_local(dt_value: datetime | None, tz) -> datetime | None:
    if dt_value is None:
        return None
    if dt_value.tzinfo is None:
        dt_value = dt_value.replace(tzinfo=timezone.utc)
    return dt_value.astimezone(tz)


@dataclass(frozen=True)
class _TodayCalendarBlock:
    events: tuple[ICloudVisibleEvent, ...]
    unavailable: bool = False


async def _fetch_today_calendar_block(
    *,
    tz: ZoneInfo,
    calendar_urls: list[str],
    icloud: ICloudCalDAVAdapter | None,
) -> _TodayCalendarBlock:
    calendar_urls = [url for url in calendar_urls if url]
    if not calendar_urls:
        return _TodayCalendarBlock(events=())
    if icloud is None:
        return _TodayCalendarBlock(events=(), unavailable=True)

    now_local = datetime.now(tz)
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)
    start_utc = start_local.astimezone(UTC)
    end_utc = end_local.astimezone(UTC)

    results = await asyncio.gather(
        *[
            icloud.list_events(calendar_url, start_utc=start_utc, end_utc=end_utc)
            for calendar_url in calendar_urls
        ],
        return_exceptions=True,
    )

    unavailable = False
    merged: dict[str, ICloudVisibleEvent] = {}
    for idx, result in enumerate(results):
        if isinstance(result, Exception):
            unavailable = True
            logger.warning(
                "today calendar fetch failed: %s",
                result,
                exc_info=(type(result), result, result.__traceback__),
            )
            continue
        logger.info(f"Calendar {idx}: received {len(result)} events from {calendar_urls[idx] if idx < len(calendar_urls) else 'unknown'}")
        for event in result:
            # Дедупликация только по uid (или уникальной комбинации)
            # Если uid нет, используем summary+время для уникальности
            # Убрали calendar_url из ключа, чтобы из одного календаря могли показываться все события
            uid_key = event.uid if event.uid else f"{event.summary}|{event.dtstart_utc.isoformat()}|{event.dtend_utc.isoformat()}"
            key = uid_key
            logger.info(f"Event: summary='{event.summary}', uid='{event.uid}', key='{key}', calendar='{event.calendar_url}'")
            if key in merged:
                logger.warning(f"Duplicate key '{key}' - replacing event '{merged[key].summary}' with '{event.summary}'")
            merged[key] = event
    logger.info(f"After deduplication: {len(merged)} unique events")
    events = tuple(sorted(merged.values(), key=lambda item: (item.dtstart_utc, item.dtend_utc, item.summary.lower())))
    return _TodayCalendarBlock(events=events, unavailable=unavailable)


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
    tz_name = tz_name or _tz_name()
    tz = resolve_tzinfo(tz_name)
    toast_line = await _pop_screen_toast(db_pool, int(message.chat.id))
    page = max(0, int(page or 0))
    page_size = 8
    work_calendar_url = (os.getenv("ICLOUD_CALENDAR_URL_WORK") or "").strip()
    personal_calendar_url = (os.getenv("ICLOUD_CALENDAR_URL_PERSONAL") or "").strip()
    bitrix_calendar_url = (os.getenv("ICLOUD_CALENDAR_URL_BITRIX") or "").strip()
    try:
        calendar_block = await _fetch_today_calendar_block(
            tz=tz,
            calendar_urls=[work_calendar_url, personal_calendar_url, bitrix_calendar_url],
            icloud=icloud,
        )
        async with db_pool.acquire() as conn:
            total_tasks = await conn.fetchval(
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
            tasks = await conn.fetch(
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
        total_tasks = int(total_tasks or 0)
        events = [
            {
                "calendar_url": event.calendar_url,
                "summary": event.summary,
                "dtstart_utc": event.dtstart_utc,
                "dtend_utc": event.dtend_utc,
            }
            for event in calendar_block.events
        ]
        total_events = len(events)

        parts = ["<b>📅 План на сегодня</b>", f"<i>Задач: {total_tasks} · Напоминаний: {len(reminders)} · Событий: {total_events}</i>"]
        if toast_line:
            parts = [toast_line, ""] + parts
        if not tasks and not reminders and not events:
            parts.extend(["", "На сегодня нет задач, напоминаний и событий."])
            if calendar_block.unavailable:
                parts.extend(["", "<i>События временно недоступны.</i>"])
            return await ui_render(
                bot=message.bot,
                db_pool=db_pool,
                chat_id=int(message.chat.id),
                text="\n".join(parts).strip(),
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="🗂 Выполнено", callback_data="nav:today:done")],
                        [InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home")],
                    ]
                ),
                screen="today",
                payload={"page": page},
                fallback_message=message,
                parse_mode="HTML",
                preferred_message_id=preferred_message_id,
                force_new=force_new,
            )

        if events:
            parts.extend(["", "<b>📅 События</b>"])
            for event_row in events[:3]:
                icon = _event_calendar_icon(
                    str(event_row.get("calendar_url") or ""),
                    work_calendar_url=work_calendar_url,
                    personal_calendar_url=personal_calendar_url,
                )
                start_local = _event_local(event_row.get("dtstart_utc"), tz)
                end_local = _event_local(event_row.get("dtend_utc"), tz)
                if start_local and end_local:
                    time_range = f"{start_local.strftime('%H:%M')}–{end_local.strftime('%H:%M')}"
                elif start_local:
                    time_range = start_local.strftime("%H:%M")
                else:
                    time_range = "—"
                parts.append(f"{icon} <b>{h(time_range)}</b> • {h(str(event_row.get('summary') or 'Без названия'))}")
            if total_events > 3:
                parts.append(f"… ещё {total_events - 3}")
        elif calendar_block.unavailable:
            parts.extend(["", "<b>📅 События</b>", "<i>События временно недоступны.</i>"])

        if tasks:
            parts.extend(["", "<i>Нажмите на задачу ниже, чтобы открыть карточку.</i>"])
        elif reminders:
            parts.extend(["", "<i>Задач на сегодня нет. Ниже только напоминания.</i>"])
        elif events:
            parts.extend(["", "<i>Задач и напоминаний на сегодня нет. Ниже только календарные события.</i>"])

        if reminders:
            head = reminders[0]
            dt_local = to_local(head.get("remind_at"), tz)
            hhmm = dt_local.strftime("%H:%M") if dt_local else "—"
            parts.extend(["", f"🔔 Ближайшее напоминание: <b>{h(hhmm)}</b> — {h(head.get('text') or '')}"])

        kb: list[list[InlineKeyboardButton]] = []
        kb.extend(_single_column_task_buttons(tasks, icon="📅", tz=tz))
        nav_row: list[InlineKeyboardButton] = []
        if page > 0:
            nav_row.append(InlineKeyboardButton(text="⬅️", callback_data=f"nav:today:{page-1}"))
        if (page + 1) * page_size < total_tasks:
            nav_row.append(InlineKeyboardButton(text="➡️", callback_data=f"nav:today:{page+1}"))
        if nav_row:
            kb.append(nav_row)
        kb.append([InlineKeyboardButton(text="🗂 Выполнено", callback_data="nav:today:done")])
        if reminders:
            kb.append([InlineKeyboardButton(text="🔔 Напоминания", callback_data="nav:reminders:0")])
        kb.append([InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home")])

        return await ui_render(
            bot=message.bot,
            db_pool=db_pool,
            chat_id=int(message.chat.id),
            text="\n".join(parts).strip(),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
            screen="today",
            payload={"page": page},
            fallback_message=message,
            preferred_message_id=preferred_message_id,
            parse_mode="HTML",
            force_new=force_new,
        )
    except Exception as e:
        return await ui_render(
            bot=message.bot,
            db_pool=db_pool,
            chat_id=int(message.chat.id),
            text=f"❌ Ошибка загрузки. Для фикса: {h(str(e))}",
            reply_markup=back_home_kb(),
            screen="home",
            fallback_message=message,
            preferred_message_id=preferred_message_id,
            force_new=force_new,
            parse_mode="HTML",
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
    """Render global active tasks list grouped by project with pagination."""
    tz_name = tz_name or _tz_name()
    tz = resolve_tzinfo(tz_name)
    chat_id = int(message.chat.id)
    toast_line = await _pop_screen_toast(db_pool, chat_id)

    try:
        page = max(0, int(page or 0))
    except Exception:
        page = 0

    valid_filters = {"all", "overdue", "today", "nodate"}
    filter_key = str(filter_key or "all").strip().lower()
    if filter_key not in valid_filters:
        filter_key = "all"

    page_size = 30
    now_utc_naive = datetime.now(UTC).replace(tzinfo=None)
    now_local = datetime.now(tz)
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)
    start_utc_naive = start_local.astimezone(UTC).replace(tzinfo=None)
    end_utc_naive = end_local.astimezone(UTC).replace(tzinfo=None)

    def _short(s: str, n: int = 30) -> str:
        s = (s or "").strip()
        return s if len(s) <= n else (s[: n - 1] + "…")

    try:
        async with db_pool.acquire() as conn:
            if filter_key == "overdue":
                total = await conn.fetchval(
                    """
                    SELECT COUNT(*)
                    FROM tasks t
                    JOIN projects p ON p.id=t.project_id
                    WHERE t.status != 'done'
                      AND t.kind != 'super'
                      AND p.status='active'
                      AND t.deadline IS NOT NULL
                      AND t.deadline < $1
                    """,
                    now_utc_naive,
                )
                rows = await conn.fetch(
                    """
                    SELECT t.id, t.title, p.code AS project, COALESCE(tm.name,'—') AS assignee, t.deadline
                    FROM tasks t
                    JOIN projects p ON p.id=t.project_id
                    LEFT JOIN team tm ON tm.id=t.assignee_id
                    WHERE t.status != 'done'
                      AND t.kind != 'super'
                      AND p.status='active'
                      AND t.deadline IS NOT NULL
                      AND t.deadline < $1
                    ORDER BY p.code ASC, t.deadline ASC NULLS LAST, t.id ASC
                    LIMIT $2 OFFSET $3
                    """,
                    now_utc_naive,
                    page_size,
                    page * page_size,
                )
            elif filter_key == "today":
                total = await conn.fetchval(
                    """
                    SELECT COUNT(*)
                    FROM tasks t
                    JOIN projects p ON p.id=t.project_id
                    WHERE t.status != 'done'
                      AND t.kind != 'super'
                      AND p.status='active'
                      AND t.deadline IS NOT NULL
                      AND t.deadline >= $1
                      AND t.deadline < $2
                    """,
                    start_utc_naive,
                    end_utc_naive,
                )
                rows = await conn.fetch(
                    """
                    SELECT t.id, t.title, p.code AS project, COALESCE(tm.name,'—') AS assignee, t.deadline
                    FROM tasks t
                    JOIN projects p ON p.id=t.project_id
                    LEFT JOIN team tm ON tm.id=t.assignee_id
                    WHERE t.status != 'done'
                      AND t.kind != 'super'
                      AND p.status='active'
                      AND t.deadline IS NOT NULL
                      AND t.deadline >= $1
                      AND t.deadline < $2
                    ORDER BY p.code ASC, t.deadline ASC NULLS LAST, t.id ASC
                    LIMIT $3 OFFSET $4
                    """,
                    start_utc_naive,
                    end_utc_naive,
                    page_size,
                    page * page_size,
                )
            elif filter_key == "nodate":
                total = await conn.fetchval(
                    """
                    SELECT COUNT(*)
                    FROM tasks t
                    JOIN projects p ON p.id=t.project_id
                    WHERE t.status != 'done'
                      AND t.kind != 'super'
                      AND p.status='active'
                      AND t.deadline IS NULL
                    """
                )
                rows = await conn.fetch(
                    """
                    SELECT t.id, t.title, p.code AS project, COALESCE(tm.name,'—') AS assignee, t.deadline
                    FROM tasks t
                    JOIN projects p ON p.id=t.project_id
                    LEFT JOIN team tm ON tm.id=t.assignee_id
                    WHERE t.status != 'done'
                      AND t.kind != 'super'
                      AND p.status='active'
                      AND t.deadline IS NULL
                    ORDER BY p.code ASC, t.deadline ASC NULLS LAST, t.id ASC
                    LIMIT $1 OFFSET $2
                    """,
                    page_size,
                    page * page_size,
                )
            else:
                total = await conn.fetchval(
                    """
                    SELECT COUNT(*)
                    FROM tasks t
                    JOIN projects p ON p.id=t.project_id
                    WHERE t.status != 'done'
                      AND t.kind != 'super'
                      AND p.status='active'
                    """
                )
                rows = await conn.fetch(
                    """
                    SELECT t.id, t.title, p.code AS project, COALESCE(tm.name,'—') AS assignee, t.deadline
                    FROM tasks t
                    JOIN projects p ON p.id=t.project_id
                    LEFT JOIN team tm ON tm.id=t.assignee_id
                    WHERE t.status != 'done'
                      AND t.kind != 'super'
                      AND p.status='active'
                    ORDER BY p.code ASC, t.deadline ASC NULLS LAST, t.id ASC
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
            text=f"❌ Ошибка загрузки. Для фикса: {h(str(e))}",
            reply_markup=back_home_kb(),
            screen="all_tasks",
            fallback_message=message,
            preferred_message_id=preferred_message_id,
            force_new=force_new,
            parse_mode="HTML",
        )

    total = int(total or 0)
    rows = list(rows or [])

    filter_titles = {
        "all": "Все",
        "overdue": "Просрочено",
        "today": "Сегодня",
        "nodate": "Без срока",
    }
    filter_title = filter_titles.get(filter_key, "Все")

    lines: list[str] = [
        "📋 <b>Все задачи</b>",
        f"<i>Фильтр: {h(filter_title)} · Всего: {total}</i>",
        "",
    ]
    if toast_line:
        lines = [toast_line, ""] + lines
    if not rows:
        lines.append("По выбранному фильтру задач нет.")
    else:
        lines.append("Нажмите на задачу ниже, чтобы открыть карточку.")

    kb: list[list[InlineKeyboardButton]] = []
    qd_suffix = ":qd1" if quick_done else ""

    filter_buttons: list[InlineKeyboardButton] = []
    filter_order = ("all", "overdue", "today", "nodate")
    for key in filter_order:
        title = filter_titles[key]
        text = f"• {title}" if key == filter_key else title
        filter_buttons.append(InlineKeyboardButton(text=text, callback_data=f"nav:all:{key}{qd_suffix}"))
    kb.append(filter_buttons)

    toggle_text = "👁 Карточки" if quick_done else "✅ Quick-Done"
    toggle_qd_suffix = "" if quick_done else ":qd1"
    kb.append([InlineKeyboardButton(text=toggle_text, callback_data=f"nav:all:{filter_key}:{page}{toggle_qd_suffix}")])

    kb.extend(_single_column_task_buttons(rows, icon="📋", tz=tz, quick_done=quick_done))

    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="⬅️", callback_data=f"nav:all:{filter_key}:{page-1}{qd_suffix}"))
    if (page + 1) * page_size < total:
        nav_row.append(InlineKeyboardButton(text="➡️", callback_data=f"nav:all:{filter_key}:{page+1}{qd_suffix}"))
    if nav_row:
        kb.append(nav_row)

    kb.append([
        InlineKeyboardButton(text="⬅️ Проекты", callback_data="nav:projects"),
        InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home"),
    ])

    return await ui_render(
        bot=message.bot,
        db_pool=db_pool,
        chat_id=chat_id,
        text="\n".join(lines).strip(),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
        screen="all_tasks",
        payload={"page": page, "filter": filter_key, "quick_done": quick_done},
        fallback_message=message,
        preferred_message_id=preferred_message_id,
        force_new=force_new,
        parse_mode="HTML",
    )


async def ui_render_work(
    message: Message,
    db_pool: asyncpg.Pool,
    *,
    tz_name: str | None = None,
    page: int = 0,
    preferred_message_id: int | None = None,
    force_new: bool = False,
) -> int:
    """Render tasks in status=in_progress (focus work list) with pagination."""
    tz_name = tz_name or _tz_name()
    tz = resolve_tzinfo(tz_name)
    chat_id = int(message.chat.id)
    toast_line = await _pop_screen_toast(db_pool, chat_id)

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

            where = "t.status='in_progress' AND t.kind != 'super'"
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
            text=f"❌ Ошибка загрузки. Для фикса: {h(str(e))}",
            reply_markup=back_home_kb(),
            screen="work",
            fallback_message=message,
            preferred_message_id=preferred_message_id,
            force_new=force_new,
            parse_mode="HTML",
        )

    rows = list(rows or [])
    total = int(total or 0)

    head = "<b>⚡ В РАБОТЕ</b>"
    if current_project_code and not current_project_is_inbox:
        head += f" — <b>{h(current_project_code)}</b>"
    lines = [head, f"<i>Всего: {total}</i>", ""]
    if toast_line:
        lines = [toast_line, ""] + lines
    if not rows:
        lines.append("Задач в работе нет.")
    else:
        lines.append("Нажмите на задачу ниже, чтобы открыть карточку.")

    # Keyboard
    kb: list[list[InlineKeyboardButton]] = []
    kb.extend(_single_column_task_buttons(rows, icon="⚡", tz=tz))

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

    return await ui_render(
        bot=message.bot,
        db_pool=db_pool,
        chat_id=chat_id,
        text="\n".join(lines).strip(),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
        screen="work",
        payload={"page": page},
        fallback_message=message,
        preferred_message_id=preferred_message_id,
        force_new=force_new,
        parse_mode="HTML",
    )


async def ui_render_inbox(
    message: Message,
    db_pool: asyncpg.Pool,
    *,
    tz_name: str | None = None,
    page: int = 0,
    preferred_message_id: int | None = None,
    force_new: bool = False,
) -> int:
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
            payload, toast_line = await _take_screen_payload(conn, chat_id)

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
                    preferred_message_id=preferred_message_id,
                    force_new=force_new,
                    parse_mode="HTML",
                )

            total = await conn.fetchval(
                "SELECT COUNT(*) FROM tasks WHERE status != 'done' AND kind != 'super' AND project_id=$1",
                int(inbox_id),
            )
            total = int(total or 0)

            rows = await conn.fetch(
                """
                SELECT t.id, t.title, COALESCE(tm.name,'—') AS assignee, t.deadline, t.created_at
                FROM tasks t
                LEFT JOIN team tm ON tm.id=t.assignee_id
                WHERE t.status != 'done' AND t.kind != 'super' AND t.project_id=$1
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
            text=f"❌ Ошибка загрузки. Для фикса: {h(str(e))}",
            reply_markup=back_home_kb(),
            screen="inbox",
            fallback_message=message,
            preferred_message_id=preferred_message_id,
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
        lines.append("Нажмите на задачу ниже или запустите разбор по одной.")

    kb: list[list[InlineKeyboardButton]] = []
    kb.append(
        [
            InlineKeyboardButton(text="🧹 Разобрать", callback_data="inbox:triage:start"),
            InlineKeyboardButton(text="⚡️ Быстрая задача", callback_data="quick:task"),
        ]
    )
    if total > 0:
        kb.append([InlineKeyboardButton(text="📁 В проект (все)", callback_data="inbox:batch:project")])

    kb.extend(_single_column_task_buttons(rows, icon="📥", project_key=None, tz=tz))

    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="⬅️", callback_data=f"nav:inbox:{page-1}"))
    if (page + 1) * page_size < total:
        nav_row.append(InlineKeyboardButton(text="➡️", callback_data=f"nav:inbox:{page+1}"))
    if nav_row:
        kb.append(nav_row)

    kb.append([
        InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home"),
    ])

    return await ui_render(
        bot=message.bot,
        db_pool=db_pool,
        chat_id=chat_id,
        text="\n".join(lines).strip(),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
        screen="inbox",
        fallback_message=message,
        preferred_message_id=preferred_message_id,
        force_new=force_new,
        parse_mode="HTML",
    )


async def ui_render_overdue(
    message: Message,
    db_pool: asyncpg.Pool,
    *,
    tz_name: str | None = None,
    page: int = 0,
    preferred_message_id: int | None = None,
    force_new: bool = False,
) -> int:
    """Render overdue tasks with pagination."""
    tz_name = tz_name or _tz_name()
    tz = resolve_tzinfo(tz_name)
    chat_id = int(message.chat.id)
    toast_line = await _pop_screen_toast(db_pool, chat_id)

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
                "SELECT COUNT(*) FROM tasks WHERE status NOT IN ('done','postponed') AND kind != 'super' AND deadline IS NOT NULL AND deadline < $1",
                now_utc_naive,
            )
            total = int(total or 0)
            rows = await conn.fetch(
                """
                SELECT t.id, t.title, p.code as project, COALESCE(tm.name,'—') as assignee, t.deadline
                FROM tasks t
                JOIN projects p ON t.project_id = p.id
                LEFT JOIN team tm ON t.assignee_id = tm.id
                WHERE t.status NOT IN ('done','postponed') AND t.kind != 'super' AND t.deadline IS NOT NULL AND t.deadline < $1
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
            text=f"❌ Ошибка загрузки. Для фикса: {h(str(e))}",
            reply_markup=back_home_kb(),
            screen="overdue",
            fallback_message=message,
            preferred_message_id=preferred_message_id,
            force_new=force_new,
            parse_mode="HTML",
        )

    rows = list(rows or [])

    lines = [f"<b>🔥 СРОЧНО</b> — просроченные задачи", f"<i>Всего: {total}</i>", ""]
    if toast_line:
        lines = [toast_line, ""] + lines
    kb: list[list[InlineKeyboardButton]] = []

    if not rows:
        lines = ([toast_line, ""] if toast_line else []) + ["🎉 <b>Просроченных задач нет.</b>"]
    else:
        lines.append("Нажмите на задачу ниже, чтобы открыть карточку.")

        kb.extend(_single_column_task_buttons(rows, icon="🔥", tz=tz))

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

    return await ui_render(
        bot=message.bot,
        db_pool=db_pool,
        chat_id=chat_id,
        text="\n".join(lines).strip(),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
        screen="overdue",
        payload={"page": page},
        fallback_message=message,
        preferred_message_id=preferred_message_id,
        force_new=force_new,
        parse_mode="HTML",
    )
