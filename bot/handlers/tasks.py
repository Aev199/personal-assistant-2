"""Task-related handlers (task card drill-down).

This module is the next step after extracting navigation + projects.
It moves the task-card drill-down out of the monolith while preserving
existing callback_data format.
"""

from __future__ import annotations

import os
import hashlib
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from bot.tz import resolve_tzinfo

import asyncpg
import dateparser
from bot.utils.datetime import parse_datetime_ru
from aiogram import Dispatcher, F
from aiogram.filters import StateFilter
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.fsm.context import FSMContext

from bot.deps import AppDeps
from bot.db import db_add_event, db_log_error
from bot.services.background import fire_and_forget
from bot.services.gtasks_service import get_or_create_list_id, due_from_local_date
from bot.services.vault_sync import background_project_sync
from bot.ui.render import ui_render
from bot.ui.state import ui_get_state, ui_set_state, _ui_payload_get, _undo_active, _now_ts, ui_payload_with_toast
from bot.ui.task_card import task_card_kb, task_deadline_kb
from bot.tz import to_db_utc
from bot.utils import h, kb_columns, try_delete_user_message





UTC = timezone.utc


def _tz_from_deps(deps: AppDeps):
    """Resolve app tzinfo for UI (prefer env; fallback to system local)."""

    return resolve_tzinfo(deps.tz_name or "Europe/Moscow")


def to_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def to_local(dt: datetime | None, tz: ZoneInfo) -> datetime | None:
    d = to_utc(dt)
    return d.astimezone(tz) if d else None


def fmt_local(dt: datetime | None, tz: ZoneInfo) -> str:
    d = to_local(dt, tz)
    return d.strftime("%d.%m %H:%M") if d else "—"

def _gtasks_fingerprint(*, project_code: str | None, title: str | None, assignee: str | None, status: str | None, deadline: datetime | None) -> str:
    """Stable fingerprint of a work task for Google Tasks sync UI.

    We store the fingerprint in DB after export. If any of these fields change,
    the export becomes "dirty" and the UI shows a refresh button.
    """

    dl = to_utc(deadline)
    dl_s = dl.isoformat() if dl else ""
    raw = "|".join([
        (project_code or "").strip(),
        (title or "").strip(),
        (assignee or "").strip(),
        (status or "").strip(),
        dl_s,
    ])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()



async def _guard(callback: CallbackQuery, deps: AppDeps) -> bool:
    if deps.admin_id and callback.from_user and callback.from_user.id != deps.admin_id:
        await callback.answer("Недоступно", show_alert=True)
        return False
    return True


def _as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _task_return_context(ui_screen: str, payload: dict) -> tuple[str | None, str | None]:
    p = payload if isinstance(payload, dict) else {}

    if ui_screen == "super_task":
        ctx = p.get("super_ctx") if isinstance(p.get("super_ctx"), dict) else {}
        super_id = _as_int(ctx.get("super_id"), 0)
        page = max(0, _as_int(ctx.get("page"), 0))
        if super_id > 0:
            return f"task:{super_id}:super:{page}", "⬅ Суперзадача"

    if ui_screen == "overdue":
        page = max(0, _as_int(p.get("page"), 0))
        return f"nav:overdue:{page}", "⬅ Просрочки"

    if ui_screen == "today":
        page = max(0, _as_int(p.get("page"), 0))
        return (f"nav:today:{page}" if page > 0 else "nav:today"), "⬅ Сегодня"

    if ui_screen == "today_pick":
        page = max(0, _as_int(p.get("page"), 0))
        return (f"nav:today:{page}" if page > 0 else "nav:today"), "⬅ Сегодня"

    if ui_screen in {"inbox", "inbox_triage"}:
        page = max(0, _as_int(p.get("inbox_page", p.get("page")), 0))
        return f"nav:inbox:{page}", "⬅ Inbox"

    if ui_screen == "work":
        page = max(0, _as_int(p.get("page"), 0))
        return f"nav:work:{page}", "⬅ В работе"

    if ui_screen == "all_tasks":
        page = max(0, _as_int(p.get("page"), 0))
        valid_filters = {"all", "overdue", "today", "nodate"}
        filter_key = str(p.get("filter") or "").strip().lower()
        if filter_key in valid_filters:
            return f"nav:all:{filter_key}:{page}", "⬅ Все задачи"
        return f"nav:all:{page}", "⬅ Все задачи"

    if ui_screen == "projects":
        return "nav:projects", "⬅ Проекты"

    if ui_screen == "project_card":
        project_id = _as_int(p.get("project_id"), 0)
        if project_id > 0:
            page = max(0, _as_int(p.get("page"), 0))
            return f"proj:{project_id}:open:{page}", "⬅ Проект"

    if ui_screen == "project_structure":
        project_id = _as_int(p.get("project_id"), 0)
        if project_id > 0:
            return f"proj:{project_id}", "⬅ Проект"

    if ui_screen == "project_tails":
        project_id = _as_int(p.get("project_id"), 0)
        if project_id > 0:
            return f"proj:{project_id}:tails", "⬅ Хвосты"

    if ui_screen == "project_tails_pick":
        project_id = _as_int(p.get("project_id"), 0)
        if project_id > 0:
            kind = str(p.get("kind") or "nodate")
            page = max(0, _as_int(p.get("page"), 0))
            return f"proj:{project_id}:tails_pick:{kind}:{page}", "⬅ Хвосты"

    if ui_screen == "team_member":
        emp_id = _as_int(p.get("emp_id"), 0)
        if emp_id > 0:
            page = max(0, _as_int(p.get("page"), 0))
            return f"team:{emp_id}:{page}", "⬅ Команда"

    if ui_screen == "tails_pick":
        kind = str(p.get("kind") or "nodate")
        page = max(0, _as_int(p.get("page"), 0))
        back = str(p.get("back") or "nav:projects")
        return f"nav:tails_pick:{kind}:{page}:{back}", "⬅ Хвосты"

    return None, None


async def _render_task_overlay(
    msg: Message,
    db_pool: asyncpg.Pool,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    parse_mode: str | None = "HTML",
    screen: str | None = None,
    payload: dict | None = None,
) -> int:
    return await ui_render(
        bot=msg.bot,
        db_pool=db_pool,
        chat_id=int(msg.chat.id),
        text=text,
        reply_markup=reply_markup,
        screen=screen,
        payload=payload,
        fallback_message=msg,
        parse_mode=parse_mode,
    )


async def show_super_task_card(msg: Message, db_pool: asyncpg.Pool, task_id: int, deps: AppDeps, *, page: int = 0) -> None:
    """Render supertask (epic) card with its child tasks (1-level hierarchy)."""
    tz = _tz_from_deps(deps)
    chat_id = int(msg.chat.id)
    page = max(0, _as_int(page, 0))
    page_size = 10

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT t.id, t.title, t.status, t.kind, p.id AS project_id, p.code AS project_code
            FROM tasks t
            JOIN projects p ON p.id=t.project_id
            WHERE t.id=$1
            """,
            int(task_id),
        )
        if not row:
            await _render_task_overlay(msg, db_pool, "❌ Суперзадача не найдена.")
            return

        kind = (row.get("kind") or "task").lower()
        if kind != "super":
            return await show_task_card(msg, db_pool, int(task_id), deps=deps)

        counts = await conn.fetchrow(
            """
            SELECT
              COUNT(*) AS total,
              COUNT(*) FILTER (WHERE status='done') AS done,
              COUNT(*) FILTER (WHERE status!='done') AS active
            FROM tasks
            WHERE parent_task_id=$1 AND kind != 'super'
            """,
            int(task_id),
        )
        total_children = int(counts.get("total") or 0) if counts else 0
        done_children = int(counts.get("done") or 0) if counts else 0
        active_children = int(counts.get("active") or 0) if counts else 0

        rows = await conn.fetch(
            """
            SELECT t.id, t.title, t.status, t.deadline, COALESCE(tm.name,'—') AS assignee
            FROM tasks t
            LEFT JOIN team tm ON tm.id=t.assignee_id
            WHERE t.parent_task_id=$1 AND t.kind != 'super' AND t.status != 'done'
            ORDER BY t.deadline ASC NULLS LAST, t.id ASC
            LIMIT $2 OFFSET $3
            """,
            int(task_id),
            page_size,
            page * page_size,
        )

        ui_state = await ui_get_state(conn, chat_id)
        payload = _ui_payload_get(ui_state)
        ui_screen = str(ui_state.get("ui_screen") or "")

        if ui_screen == "super_task":
            ctx = payload.get("super_ctx") if isinstance(payload, dict) else {}
            return_cb = (ctx.get("return_cb") or "").strip() or None
            return_label = (ctx.get("return_label") or "").strip() or None
        else:
            return_cb, return_label = _task_return_context(ui_screen, payload)

        project_id = int(row["project_id"])
        project_code = str(row.get("project_code") or "").strip()
        if not return_cb:
            return_cb = f"proj:{project_id}"
            return_label = "⬅ Проект"

        if not isinstance(payload, dict):
            payload = {}
        payload["super_ctx"] = {
            "super_id": int(task_id),
            "page": int(page),
            "return_cb": return_cb,
            "return_label": return_label or "⬅ Назад",
        }

    title = (row.get("title") or "").strip()
    status = (row.get("status") or "todo").lower()
    status_txt = "закрыта" if status == "done" else "активна"

    def _short(s: str, n: int = 36) -> str:
        s = (s or "").strip()
        return s if len(s) <= n else (s[: n - 1] + "…")

    lines: list[str] = [
        f"🧩 <b>Суперзадача</b> #{int(task_id)}",
        f"Проект: <b>{h(project_code)}</b>",
        f"Статус: <b>{h(status_txt)}</b>",
        f"Готово: <b>{done_children}/{total_children}</b>",
        "",
        f"🧩 {h(title)}",
        "",
        "<b>Задачи внутри:</b>",
    ]

    if not rows:
        lines.append("—")
    else:
        for r in rows:
            t_title = (r.get("title") or "").strip()
            assignee = (r.get("assignee") or "—").strip()
            dl_local = to_local(r.get("deadline"), tz)
            due = dl_local.strftime("%d.%m %H:%M") if dl_local else "без срока"
            lines.append(f"• {h(t_title)} — {h(assignee)}, <i>{h('до ' + due) if dl_local else h(due)}</i>")

    kb: list[list[InlineKeyboardButton]] = []
    kb.append(
        [
            InlineKeyboardButton(text="➕ Задача", callback_data=f"add:sub:{int(task_id)}"),
            InlineKeyboardButton(text="✅ Закрыть", callback_data=f"task:{int(task_id)}:super_close_ask"),
        ]
    )

    task_buttons: list[InlineKeyboardButton] = []
    for r in rows:
        t_title = (r.get("title") or "").strip()
        task_buttons.append(InlineKeyboardButton(text=_short(t_title, 24), callback_data=f"task:{int(r['id'])}"))
    kb.extend(kb_columns(task_buttons, 2))

    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="⬅️", callback_data=f"task:{int(task_id)}:super:{page-1}"))
    if (page + 1) * page_size < active_children:
        nav_row.append(InlineKeyboardButton(text="➡️", callback_data=f"task:{int(task_id)}:super:{page+1}"))
    if nav_row:
        kb.append(nav_row)

    kb.append(
        [
            InlineKeyboardButton(text=(return_label or "⬅ Назад"), callback_data=str(return_cb)),
            InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home"),
        ]
    )

    await ui_render(
        bot=msg.bot,
        db_pool=db_pool,
        chat_id=int(msg.chat.id),
        text="\n".join(lines).strip(),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
        screen="super_task",
        payload=payload,
        fallback_message=msg,
        parse_mode="HTML",
    )


async def build_task_card(
    *,
    db_pool: asyncpg.Pool,
    chat_id: int,
    task_id: int,
    deps: AppDeps,
    expanded: bool = False,
) -> tuple[str, InlineKeyboardMarkup | None, str]:
    """Build task card UI without sending/editing messages.

    Returns: (text, keyboard, kind) where kind is one of: task/super/missing.
    """
    tz = _tz_from_deps(deps)
    kind = "task"
    row: dict | None = None
    subs: list[dict] = []
    return_cb: str | None = None
    return_label: str | None = None
    undo: dict | None = None
    triage_active = False
    inbox_left: int | None = None

    async with db_pool.acquire() as conn:
        rec = await conn.fetchrow(
            """
            SELECT t.id, t.title, t.status, t.kind, t.deadline, t.parent_task_id,
                   t.g_task_id, t.g_task_list_id, t.g_task_hash,
                   p.id AS project_id, p.code AS project_code,
                   COALESCE(tm.name, '—') AS assignee
            FROM tasks t
            JOIN projects p ON p.id = t.project_id
            LEFT JOIN team tm ON tm.id = t.assignee_id
            WHERE t.id=$1
            """,
            int(task_id),
        )
        if not rec:
            return "❌ Задача не найдена.", None, "missing"

        row = dict(rec)
        kind = (row.get("kind") or "task").lower()
        if kind == "super":
            return "", None, "super"

        subs = [dict(r) for r in await conn.fetch("SELECT id, title, status FROM tasks WHERE parent_task_id=$1 ORDER BY id", int(task_id))]

        ui_state = await ui_get_state(conn, int(chat_id))
        payload = _ui_payload_get(ui_state)
        ui_screen = str(ui_state.get("ui_screen") or "")
        return_cb, return_label = _task_return_context(ui_screen, payload)
        undo = _undo_active(payload, task_id=int(task_id))

        triage = payload.get("triage") if isinstance(payload, dict) else None
        triage_active = bool(
            isinstance(triage, dict)
            and triage.get("active")
            and triage.get("mode") == "inbox"
            and int(triage.get("anchor_id") or 0) == int(task_id)
        )
        if triage_active:
            try:
                inbox_id = int(triage.get("inbox_id") or 0)
                if inbox_id:
                    inbox_left = await conn.fetchval(
                        "SELECT COUNT(*) FROM tasks WHERE status != 'done' AND kind != 'super' AND project_id=$1",
                        inbox_id,
                    )
                    inbox_left = int(inbox_left or 0)
            except Exception:
                inbox_left = None

    assert row is not None

    # Convert stored naive-UTC deadline to local time for display.
    dl = fmt_local(row.get("deadline"), tz)
    if getattr(deps, "logger", None):
        try:
            raw = row.get("deadline")
            loc = to_local(raw, tz)
            deps.logger.info(
                "task card tz debug",
                task_id=int(task_id),
                deps_tz=getattr(deps, "tz_name", None),
                tz=str(tz),
                deadline_raw=str(raw),
                deadline_local=str(loc) if loc else None,
            )
        except Exception:
            pass

    status = (row.get("status") or "todo").lower()
    status_map = {
        "todo": "к выполнению",
        "in_progress": "в работе",
        "postponed": "отложено",
        "blocked": "отложено",
        "done": "готово",
    }
    st = status_map.get(status, status)

    lines = [
        f"📝 <b>ЗАДАЧА</b> #{int(row['id'])}",
        f"Проект: <b>{h(str(row.get('project_code') or ''))}</b>",
        f"Исполнитель: <b>{h(str(row.get('assignee') or '—'))}</b>",
        f"Статус: <b>{h(str(st))}</b>",
        f"Дедлайн: <b>{h(str(dl))}</b>",
    ]

    if triage_active:
        left_txt = f"{int(inbox_left)}" if inbox_left is not None else "…"
        lines.insert(0, f"📥 <b>Разбор Inbox</b> • осталось: <b>{left_txt}</b>")

    if undo:
        left = max(0, int(undo.get("exp", 0)) - _now_ts())
        lines.append(f"↩️ <i>Можно отменить последнее действие</i> (<b>{left}</b> сек)")

    lines += ["", f"Текст: {h(str(row.get('title') or ''))}"]

    if row.get("parent_task_id"):
        lines.append(f"Родитель: #{int(row['parent_task_id'])}")

    if subs:
        lines.append("")
        lines.append("<b>Подзадачи:</b>")
        for s in subs[:10]:
            mark = "✅" if s.get("status") == "done" else "•"
            lines.append(f"{mark} #{int(s['id'])} {h(str(s.get('title') or ''))}")
        if len(subs) > 10:
            lines.append(f"…и ещё {len(subs)-10}")

    active_subs = [(int(s["id"]), str(s.get("title") or "")) for s in (subs or []) if (s.get("status") != "done")]

    in_gtasks = bool(row.get("g_task_id"))
    fp = _gtasks_fingerprint(
        project_code=str(row.get("project_code") or ""),
        title=str(row.get("title") or ""),
        assignee=str(row.get("assignee") or ""),
        status=str(row.get("status") or ""),
        deadline=row.get("deadline"),
    )
    gtasks_dirty = bool(in_gtasks and (not row.get("g_task_hash") or str(row.get("g_task_hash")) != fp))
    kb = task_card_kb(
        int(task_id),
        int(row["project_id"]),
        int(row["parent_task_id"]) if row.get("parent_task_id") else None,
        str(status),
        in_gtasks=in_gtasks,
        gtasks_dirty=gtasks_dirty,
        expanded=expanded,
        subtasks=active_subs,
        is_inbox=(str(row.get("project_code") or "").upper() == "INBOX"),
        triage=triage_active,
        return_cb=return_cb,
        return_label=return_label,
    )

    if undo:
        kb.inline_keyboard.insert(0, [InlineKeyboardButton(text="↩️ Undo", callback_data=f"undo:task:{task_id}")])

    return "\n".join(lines), kb, kind


async def show_task_card(msg: Message, db_pool: asyncpg.Pool, task_id: int, deps: AppDeps, *, expanded: bool = False) -> None:
    """Render task card into the current message."""
    text, kb, kind = await build_task_card(db_pool=db_pool, chat_id=int(msg.chat.id), task_id=int(task_id), deps=deps, expanded=expanded)
    if kind == "super":
        return await show_super_task_card(msg, db_pool, int(task_id), deps=deps, page=0)
    await ui_render(
        bot=msg.bot,
        db_pool=db_pool,
        chat_id=int(msg.chat.id),
        text=text,
        reply_markup=kb,
        screen=None,
        payload=None,
        fallback_message=msg,
        parse_mode="HTML",
    )


async def _advance_inbox_triage_after_action(msg: Message, db_pool: asyncpg.Pool, deps: AppDeps, *, task_id: int) -> bool:
    chat_id = int(msg.chat.id)
    async with db_pool.acquire() as conn:
        ui_state = await ui_get_state(conn, chat_id)
        payload = _ui_payload_get(ui_state)
        triage = payload.get("triage") if isinstance(payload, dict) else None
        if not isinstance(triage, dict) or not triage.get("active") or triage.get("mode") != "inbox":
            return False
        if int(triage.get("anchor_id") or 0) != int(task_id):
            return False

        inbox_id = int(triage.get("inbox_id") or 0)
        if inbox_id <= 0:
            payload.pop("triage", None)
            payload = ui_payload_with_toast(payload, "❌ Не найден INBOX", ttl_sec=20)
            await ui_set_state(conn, chat_id, ui_payload=payload)
            from bot.ui.screens import ui_render_home

            await ui_render_home(msg, db_pool, tz_name=deps.tz_name)
            return True

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
            inbox_id,
            anchor_created_at,
            int(task_id),
        )

        if not nxt:
            payload.pop("triage", None)
            payload = ui_payload_with_toast(payload, "🎉 Inbox разобран", ttl_sec=25)
            await ui_set_state(conn, chat_id, ui_payload=payload)
            return_to = str(triage.get("return") or "inbox")
            from bot.ui.screens import ui_render_home, ui_render_inbox

            if return_to == "home":
                await ui_render_home(msg, db_pool, tz_name=deps.tz_name)
            else:
                await ui_render_inbox(msg, db_pool, tz_name=deps.tz_name, page=0)
            return True

        triage["anchor_created_at"] = nxt["created_at"].isoformat()
        triage["anchor_id"] = int(nxt["id"])
        payload["triage"] = triage
        await ui_set_state(conn, chat_id, ui_screen="inbox_triage", ui_payload=payload)
        next_task_id = int(nxt["id"])

    await show_task_card(msg, db_pool, next_task_id, deps=deps)
    return True


async def cb_undo_task(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if not await _guard(callback, deps):
        return
    await callback.answer()
    await state.clear()

    vault = deps.vault

    parts = (callback.data or "").split(":")
    if len(parts) < 3 or not parts[2].isdigit():
        return
    task_id = int(parts[2])
    chat_id = int(callback.message.chat.id)

    try:
        async with db_pool.acquire() as conn:
            ui_state = await ui_get_state(conn, chat_id)
            payload = _ui_payload_get(ui_state)
            undo = _undo_active(payload, task_id=task_id)
            if not undo:
                payload.pop("undo", None)
                await ui_set_state(conn, chat_id, ui_payload=payload)
                await show_task_card(callback.message, db_pool, task_id, deps=deps)
                return

            prev_status = undo.get("prev_status") or "todo"

            info = await conn.fetchrow(
                "SELECT t.project_id, p.code as project_code, t.title, t.status "
                "FROM tasks t JOIN projects p ON p.id=t.project_id WHERE t.id=$1",
                task_id,
            )
            if not info:
                payload.pop("undo", None)
                await ui_set_state(conn, chat_id, ui_payload=payload)
                await _render_task_overlay(callback.message, db_pool, "❌ Задача не найдена.", parse_mode="HTML")
                return

            cur_status = info["status"] or "todo"
            if undo.get("new_status") and cur_status != undo.get("new_status"):
                payload.pop("undo", None)
                await ui_set_state(conn, chat_id, ui_payload=payload)
                await show_task_card(callback.message, db_pool, task_id, deps=deps)
                return

            await conn.execute("UPDATE tasks SET status=$2 WHERE id=$1", task_id, prev_status)
            await db_add_event(
                conn,
                "task_undo",
                int(info["project_id"]),
                task_id,
                f"↩️ Отмена | [{info['project_code']}] #{task_id} {info['title']} (статус → {prev_status})",
            )

            payload.pop("undo", None)
            await ui_set_state(conn, chat_id, ui_payload=payload)

        fire_and_forget(
            background_project_sync(int(info["project_id"]), db_pool, vault, error_logger=lambda w, e, c: db_log_error(db_pool, w, e, c)),
            label="vault_sync",
        )
        await show_task_card(callback.message, db_pool, task_id, deps=deps)
    except Exception as e:
        await callback.answer("❌ Ошибка Undo", show_alert=True)
        await db_log_error(db_pool, "cb_undo_task", e, {"task_id": task_id, "chat_id": chat_id})


async def cb_task(
    callback: CallbackQuery,
    state: FSMContext,
    db_pool: asyncpg.Pool,
    deps: AppDeps,
) -> None:
    if not await _guard(callback, deps):
        return

    await callback.answer()
    await state.clear()

    vault = deps.vault
    gtasks = deps.gtasks

    parts = (callback.data or "").split(":")
    if len(parts) < 2 or not parts[1].isdigit():
        return

    task_id = int(parts[1])
    action = parts[2] if len(parts) >= 3 else "open"

    # -----------------
    # Card display
    # -----------------
    if action == "open":
        return await show_task_card(callback.message, db_pool, task_id, deps=deps)
    if action == "more":
        return await show_task_card(callback.message, db_pool, task_id, deps=deps, expanded=True)
    if action == "less":
        return await show_task_card(callback.message, db_pool, task_id, deps=deps, expanded=False)
    if action == "super":
        page = int(parts[3]) if len(parts) >= 4 and parts[3].isdigit() else 0
        return await show_super_task_card(callback.message, db_pool, task_id, deps=deps, page=page)
    if action == "super_close_ask":
        async with db_pool.acquire() as conn:
            ui_state = await ui_get_state(conn, int(callback.message.chat.id))
            payload = _ui_payload_get(ui_state)
        ctx = payload.get("super_ctx") if isinstance(payload, dict) else {}
        page = max(0, _as_int(ctx.get("page"), 0))
        return_cb = (ctx.get("return_cb") or "nav:home")
        return_label = (ctx.get("return_label") or "⬅ Назад")
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ Да, закрыть все", callback_data=f"task:{task_id}:super_close_do")],
                [InlineKeyboardButton(text="⬅ Отмена", callback_data=f"task:{task_id}:super:{page}")],
                [
                    InlineKeyboardButton(text=str(return_label), callback_data=str(return_cb)),
                    InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home"),
                ],
            ]
        )
        return await _render_task_overlay(
            callback.message,
            db_pool,
            "✅ <b>Закрыть суперзадачу?</b>\n\nЭто действие отметит <b>done</b> суперзадачу и все задачи внутри.",
            reply_markup=kb,
            parse_mode="HTML",
        )
    if action == "super_close_do":
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT t.id, t.title, t.kind, t.project_id, p.code AS project_code "
                "FROM tasks t JOIN projects p ON p.id=t.project_id WHERE t.id=$1",
                int(task_id),
            )
            if not row or (row.get("kind") or "task") != "super":
                await callback.answer("Суперзадача не найдена", show_alert=True)
                return await show_task_card(callback.message, db_pool, task_id, deps=deps)
            closed_rows = await conn.fetch(
                "UPDATE tasks SET status='done' "
                "WHERE parent_task_id=$1 AND status != 'done' AND kind != 'super' "
                "RETURNING id",
                int(task_id),
            )
            closed_n = len(list(closed_rows or []))
            await conn.execute(
                "UPDATE tasks SET status='done', assignee_id=NULL, deadline=NULL, parent_task_id=NULL WHERE id=$1",
                int(task_id),
            )
            await db_add_event(
                conn,
                "super_task_closed",
                int(row["project_id"]),
                int(task_id),
                f"✅ Закрыта суперзадача | [{row['project_code']}] #{int(task_id)} {row['title']} (закрыто задач: {closed_n})",
            )
            ui_state = await ui_get_state(conn, int(callback.message.chat.id))
            payload = _ui_payload_get(ui_state)

        fire_and_forget(
            background_project_sync(int(row["project_id"]), db_pool, vault, error_logger=lambda w, e, c: db_log_error(db_pool, w, e, c)),
            label="vault_sync",
        )
        ctx = payload.get("super_ctx") if isinstance(payload, dict) else {}
        return_cb = (ctx.get("return_cb") or f"proj:{int(row['project_id'])}")
        return_label = (ctx.get("return_label") or "⬅ Проект")
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=str(return_label), callback_data=str(return_cb))],
                [InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home")],
            ]
        )
        return await _render_task_overlay(
            callback.message,
            db_pool,
            f"✅ Суперзадача закрыта. Закрыто задач: <b>{closed_n}</b>.",
            reply_markup=kb,
            parse_mode="HTML",
        )
    if action == "dlcancel":
        await state.clear()
        return await show_task_card(callback.message, db_pool, task_id, deps=deps)

    # Block unsupported actions on supertasks (old callbacks, direct links).
    async with db_pool.acquire() as conn:
        kind = await conn.fetchval("SELECT kind FROM tasks WHERE id=$1", int(task_id))
    if (kind or "task") == "super":
        await callback.answer("Это суперзадача: действие недоступно", show_alert=True)
        async with db_pool.acquire() as conn:
            ui_state = await ui_get_state(conn, int(callback.message.chat.id))
            payload = _ui_payload_get(ui_state)
        ctx = payload.get("super_ctx") if isinstance(payload, dict) else {}
        page = max(0, _as_int(ctx.get("page"), 0))
        return await show_super_task_card(callback.message, db_pool, task_id, deps=deps, page=page)

    # -----------------
    # Inbox triage: move task to another project
    # -----------------
    if action == "move":
        async with db_pool.acquire() as conn:
            info = await conn.fetchrow("SELECT t.project_id FROM tasks t WHERE t.id=$1", task_id)
            if not info:
                return await _render_task_overlay(callback.message, db_pool, "❌ Задача не найдена.")
            rows = await conn.fetch("SELECT id, code FROM projects WHERE status='active' ORDER BY code")
        cur_pid = int(info["project_id"])
        kb_rows: list[list[InlineKeyboardButton]] = []
        row_buf: list[InlineKeyboardButton] = []
        for r in rows:
            pid = int(r["id"])
            if pid == cur_pid:
                continue
            row_buf.append(InlineKeyboardButton(text=str(r["code"]), callback_data=f"task:{task_id}:move_to:{pid}"))
            if len(row_buf) == 2:
                kb_rows.append(row_buf)
                row_buf = []
        if row_buf:
            kb_rows.append(row_buf)
        kb_rows.append([
            InlineKeyboardButton(text="⬅ Назад", callback_data=f"task:{task_id}:more"),
            InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home"),
        ])
        return await _render_task_overlay(
            callback.message,
            db_pool,
            "📁 <b>Выберите проект:</b>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
            parse_mode="HTML",
        )

    if action == "move_to" and len(parts) >= 4 and parts[3].isdigit():
        new_pid = int(parts[3])
        async with db_pool.acquire() as conn:
            info = await conn.fetchrow(
                "SELECT t.project_id, t.title, p.code as old_code FROM tasks t JOIN projects p ON p.id=t.project_id WHERE t.id=$1",
                task_id,
            )
            if not info:
                return await _render_task_overlay(callback.message, db_pool, "❌ Задача не найдена.")
            old_pid = int(info["project_id"])
            if new_pid == old_pid:
                return await show_task_card(callback.message, db_pool, task_id, deps=deps, expanded=True)
            new_code = await conn.fetchval("SELECT code FROM projects WHERE id=$1", new_pid)
            await conn.execute("UPDATE tasks SET project_id=$2 WHERE id=$1", task_id, new_pid)
            await db_add_event(conn, "task_move", new_pid, task_id, f"Перенесено из [{info['old_code']}] в [{new_code}]")

        fire_and_forget(
            background_project_sync(old_pid, db_pool, vault, error_logger=lambda w, e, c: db_log_error(db_pool, w, e, c)),
            label="vault_sync",
        )
        fire_and_forget(
            background_project_sync(new_pid, db_pool, vault, error_logger=lambda w, e, c: db_log_error(db_pool, w, e, c)),
            label="vault_sync",
        )
        if await _advance_inbox_triage_after_action(callback.message, db_pool, deps, task_id=task_id):
            return
        return await show_task_card(callback.message, db_pool, task_id, deps=deps, expanded=True)

    # -----------------
    # Export work task to Google Tasks (fallback)
    # -----------------
    if action == "gtasks":
        if not gtasks.enabled():
            await callback.answer("❌ Google Tasks не настроен", show_alert=True)
            return await show_task_card(callback.message, db_pool, task_id, deps=deps)

        tz = _tz_from_deps(deps)

        def _is_not_found(err: BaseException) -> bool:
            # Adapter raises RuntimeError("Google Tasks error (404): ...")
            msg = str(err)
            return ("(404)" in msg) or ("(410)" in msg) or ("notFound" in msg) or ("Not Found" in msg) or ("Resource Not Found" in msg)

        async def _clear_task_mapping() -> None:
            async with db_pool.acquire() as conn:
                await conn.execute("UPDATE tasks SET g_task_id=NULL, g_task_list_id=NULL, g_task_hash=NULL, g_task_synced_at=NULL WHERE id=$1", task_id)

        async def _forget_list_mapping(name: str) -> None:
            async with db_pool.acquire() as conn:
                await conn.execute("DELETE FROM g_tasks_lists WHERE name=$1", name)

        async def _create_task_with_list_retry(list_name: str, list_id: str, title: str, notes: str, due_utc, fp: str) -> str | None:
            cur_list_id = list_id
            for attempt in range(2):
                try:
                    created = await gtasks.create_task(cur_list_id, title, notes=notes, due=due_utc)
                    gtid = created.get("id")
                    if gtid:
                        async with db_pool.acquire() as conn:
                            await conn.execute(
                                "UPDATE tasks SET g_task_id=$2, g_task_list_id=$3, g_task_hash=$4, g_task_synced_at=NOW() WHERE id=$1",
                                task_id,
                                str(gtid),
                                str(cur_list_id),
                                str(fp),
                            )
                        return str(gtid)
                    return None
                except Exception as e:
                    # If the tasklist was deleted, refresh mapping and retry once.
                    if attempt == 0 and _is_not_found(e):
                        await _forget_list_mapping(list_name)
                        cur_list_id = await get_or_create_list_id(db_pool, gtasks, list_name)
                        continue
                    raise
            return None

        try:
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT t.id, t.title, t.deadline, t.status, t.g_task_id, t.g_task_list_id, t.g_task_hash,
                           p.code AS project_code,
                           COALESCE(tm.name,'—') AS assignee
                    FROM tasks t
                    JOIN projects p ON p.id=t.project_id
                    LEFT JOIN team tm ON tm.id=t.assignee_id
                    WHERE t.id=$1
                    """,
                    task_id,
                )
            if not row:
                await callback.answer("❌ Задача не найдена", show_alert=True)
                return

            fp = _gtasks_fingerprint(
                project_code=str(row.get("project_code") or ""),
                title=str(row.get("title") or ""),
                assignee=str(row.get("assignee") or ""),
                status=str(row.get("status") or ""),
                deadline=row.get("deadline"),
            )

            list_name = (row["project_code"] or "").strip() or os.getenv("GTASKS_WORK_FALLBACK_LIST", "Работа")
            list_id = await get_or_create_list_id(db_pool, gtasks, list_name)

            # Make updates visible in the Google Tasks list view:
            # include assignee + local deadline in title (can be disabled via env).
            dl = fmt_local(row["deadline"], tz) if row["deadline"] else "—"
            assignee = str(row.get("assignee") or "—")

            show_meta = (os.getenv("GTASKS_TITLE_META", "1").strip() not in {"0", "false", "False"})
            meta_parts: list[str] = []
            if show_meta:
                if assignee and assignee != "—":
                    meta_parts.append(assignee)
                if dl and dl != "—":
                    meta_parts.append(dl)
            meta = f" • {' • '.join(meta_parts)}" if meta_parts else ""

            base = f"[{row['project_code']}] #{task_id} {row['title']}" if row["project_code"] else f"#{task_id} {row['title']}"
            title = f"{base}{meta}"
            notes = f"Проект: {row['project_code']}\nИсполнитель: {assignee}\nДедлайн: {dl}\nID: {task_id}"

            due_utc = None
            if row["deadline"]:
                due_utc = due_from_local_date(to_local(row["deadline"], tz), tz)

            if row["g_task_id"] and row["g_task_list_id"]:
                try:
                    list_id_existing = str(row["g_task_list_id"])
                    task_id_existing = str(row["g_task_id"])

                    # 1) Try PATCH
                    await gtasks.patch_task(list_id_existing, task_id_existing, title=title, notes=notes, due=due_utc)

                    # 2) Verify (some tenants/UIs are flaky with PATCH)
                    try:
                        got = await gtasks.get_task(list_id_existing, task_id_existing)
                        exp_due = gtasks._fmt_due(due_utc) if due_utc else None
                        got_due = got.get("due")
                        if (got.get("title") != title) or (got.get("notes") != notes) or (exp_due and got_due != exp_due):
                            body = {
                                "kind": got.get("kind") or "tasks#task",
                                "id": got.get("id") or task_id_existing,
                                "title": title,
                                "notes": notes,
                                "status": got.get("status") or "needsAction",
                            }
                            if exp_due:
                                body["due"] = exp_due
                            # Preserve completion timestamp if present
                            if got.get("completed"):
                                body["completed"] = got.get("completed")
                            await gtasks.update_task(list_id_existing, task_id_existing, body)
                    except Exception:
                        # Verification is best-effort; do not fail the UX.
                        pass

                    async with db_pool.acquire() as conn:
                        await conn.execute("UPDATE tasks SET g_task_hash=$2, g_task_synced_at=NOW() WHERE id=$1", task_id, fp)
                    await callback.answer("✅ Google Tasks: обновлено")
                except Exception as e:
                    # Common case: task was deleted manually from Google Tasks.
                    if _is_not_found(e):
                        await _clear_task_mapping()
                        gtid = await _create_task_with_list_retry(list_name, list_id, title, notes, due_utc, fp)
                        if gtid:
                            await callback.answer("✅ Google Tasks: отправлено заново")
                        else:
                            await callback.answer("✅ Google Tasks: отправлено")
                    else:
                        raise
            else:
                gtid = await _create_task_with_list_retry(list_name, list_id, title, notes, due_utc, fp)
                if gtid:
                    await callback.answer("✅ Google Tasks: отправлено")
                else:
                    await callback.answer("✅ Google Tasks: отправлено")
        except Exception as e:
            await callback.answer("❌ Ошибка Google Tasks", show_alert=True)
            await db_log_error(db_pool, "cb_task_gtasks", e, {"task_id": task_id})
        return await show_task_card(callback.message, db_pool, task_id, deps=deps)


    # -----------------
    # Status updates
    # -----------------
    if action in {"done", "in_progress", "postpone", "blocked"}:
        new_status = "postponed" if action in {"postpone", "blocked"} else action
        gtask_to_complete: tuple[str, str] | None = None
        chat_id = int(callback.message.chat.id)

        async with db_pool.acquire() as conn:
            info = await conn.fetchrow(
                "SELECT t.project_id, p.code as project_code, t.title, t.status "
                "FROM tasks t JOIN projects p ON p.id=t.project_id WHERE t.id=$1",
                task_id,
            )
            if not info:
                return await _render_task_overlay(callback.message, db_pool, "❌ Задача не найдена.")
            pid = int(info["project_id"])
            prev_status = info["status"] or "todo"

            await conn.execute("UPDATE tasks SET status=$2 WHERE id=$1", task_id, new_status)

            if new_status == "done" and gtasks.enabled():
                g = await conn.fetchrow("SELECT g_task_id, g_task_list_id FROM tasks WHERE id=$1", task_id)
                if g and g["g_task_id"] and g["g_task_list_id"]:
                    gtask_to_complete = (str(g["g_task_list_id"]), str(g["g_task_id"]))

            if new_status == "done":
                await db_add_event(conn, "task_done", pid, task_id, f"✅ Закрыто | [{info['project_code']}] #{task_id} {info['title']}")
            elif new_status == "postponed":
                await db_add_event(conn, "task_postponed", pid, task_id, f"⏸ Отложено | [{info['project_code']}] #{task_id} {info['title']}")
            elif new_status == "in_progress":
                await db_add_event(conn, "task_in_progress", pid, task_id, f"⏳ В работе | [{info['project_code']}] #{task_id} {info['title']}")
            else:
                await db_add_event(conn, "task_status", pid, task_id, f"Статус → {new_status} | [{info['project_code']}] #{task_id} {info['title']}")

            ui_state = await ui_get_state(conn, chat_id)
            payload = _ui_payload_get(ui_state)
            payload["undo"] = {
                "type": "task_status",
                "task_id": int(task_id),
                "prev_status": str(prev_status),
                "new_status": str(new_status),
                "exp": _now_ts() + 30,
            }
            await ui_set_state(conn, chat_id, ui_payload=payload)

        fire_and_forget(
            background_project_sync(pid, db_pool, vault, error_logger=lambda w, e, c: db_log_error(db_pool, w, e, c)),
            label="vault_sync",
        )

        if gtask_to_complete:
            async def _mark_done(list_id: str, task_id_str: str) -> None:
                try:
                    await gtasks.patch_task(list_id, task_id_str, completed=True)
                except Exception:
                    pass

            fire_and_forget(_mark_done(gtask_to_complete[0], gtask_to_complete[1]), label="gtasks_done")
        if await _advance_inbox_triage_after_action(callback.message, db_pool, deps, task_id=task_id):
            return
        return await show_task_card(callback.message, db_pool, task_id, deps=deps)

    # -----------------
    # Assignee
    # -----------------
    if action == "assignee":
        async with db_pool.acquire() as conn:
            pid = await conn.fetchval("SELECT project_id FROM tasks WHERE id=$1", task_id)
            if not pid:
                return await _render_task_overlay(callback.message, db_pool, "❌ Задача не найдена.")
            team = await conn.fetch("SELECT id, name FROM team ORDER BY name")

        kb: list[list[InlineKeyboardButton]] = [
            [InlineKeyboardButton(text="Без исполнителя", callback_data=f"task:{task_id}:assignee_set:none")]
        ]
        member_buttons = [
            InlineKeyboardButton(text=r["name"], callback_data=f"task:{task_id}:assignee_set:{r['id']}") for r in team
        ]
        kb.extend(kb_columns(member_buttons, 2))
        kb.append([
            InlineKeyboardButton(text="⬅ Назад", callback_data=f"task:{task_id}"),
            InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home"),
        ])
        return await _render_task_overlay(
            callback.message,
            db_pool,
            "👤 Выберите исполнителя:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
        )

    if action == "assignee_set" and len(parts) >= 4:
        token = parts[3]
        new_assignee = None if token == "none" else int(token)
        async with db_pool.acquire() as conn:
            info = await conn.fetchrow(
                "SELECT t.project_id, p.code as project_code, t.title FROM tasks t JOIN projects p ON p.id=t.project_id WHERE t.id=$1",
                task_id,
            )
            if not info:
                return await _render_task_overlay(callback.message, db_pool, "❌ Задача не найдена.")
            pid = int(info["project_id"])
            await conn.execute("UPDATE tasks SET assignee_id=$2 WHERE id=$1", task_id, new_assignee)
            nm = "—"
            if new_assignee is not None:
                nm = await conn.fetchval("SELECT name FROM team WHERE id=$1", new_assignee) or "—"
            await db_add_event(conn, "task_assignee_changed", pid, task_id, f"👤 Исполнитель → {nm} | [{info['project_code']}] #{task_id} {info['title']}")

        fire_and_forget(
            background_project_sync(pid, db_pool, vault, error_logger=lambda w, e, c: db_log_error(db_pool, w, e, c)),
            label="vault_sync",
        )
        return await show_task_card(callback.message, db_pool, task_id, deps=deps)

    # -----------------
    # Parent / subtask relations
    # -----------------
    if action == "detach":
        async with db_pool.acquire() as conn:
            info = await conn.fetchrow(
                "SELECT t.project_id, p.code as project_code, t.title, t.parent_task_id "
                "FROM tasks t JOIN projects p ON p.id=t.project_id WHERE t.id=$1",
                task_id,
            )
            if not info:
                return await _render_task_overlay(callback.message, db_pool, "❌ Задача не найдена.")
            pid = int(info["project_id"])
            old_parent = info["parent_task_id"]
            await conn.execute("UPDATE tasks SET parent_task_id=NULL WHERE id=$1", task_id)
            await db_add_event(conn, "task_parent_unset", pid, task_id, f"⛓ Отвязано от родителя | [{info['project_code']}] #{task_id} {info['title']} (был #{old_parent})")

        fire_and_forget(
            background_project_sync(pid, db_pool, vault, error_logger=lambda w, e, c: db_log_error(db_pool, w, e, c)),
            label="vault_sync",
        )
        return await show_task_card(callback.message, db_pool, task_id, deps=deps)

    if action == "parent":
        page = 0
        if len(parts) >= 4 and parts[3].isdigit():
            page = max(0, int(parts[3]))
        page_size = 8
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT project_id FROM tasks WHERE id=$1", task_id)
            if not row:
                return await _render_task_overlay(callback.message, db_pool, "❌ Задача не найдена.")
            project_id = int(row["project_id"])
            total = await conn.fetchval(
                "SELECT COUNT(*) FROM tasks WHERE project_id=$1 AND status != 'done' AND kind='super' AND id != $2",
                project_id,
                int(task_id),
            )
            total = int(total or 0)
            rows = await conn.fetch(
                """
                SELECT id, title
                FROM tasks
                WHERE project_id=$1 AND status != 'done' AND kind='super' AND id != $2
                ORDER BY id
                LIMIT $3 OFFSET $4
                """,
                project_id,
                int(task_id),
                page_size,
                page * page_size,
            )

        def _short(s: str, n: int = 36) -> str:
            s = (s or "").strip()
            return s if len(s) <= n else (s[: n - 1] + "…")

        lines = ["🧩 Выберите суперзадачу-родителя:"]
        kb: list[list[InlineKeyboardButton]] = []
        if not rows:
            lines.append("Суперзадач в проекте нет.")
        else:
            for r in rows:
                kb.append([
                    InlineKeyboardButton(
                        text=f"[{int(r['id'])}] {_short(str(r['title'] or ''), 30)}",
                        callback_data=f"task:{task_id}:parent_set:{int(r['id'])}",
                    )
                ])

        nav_row: list[InlineKeyboardButton] = []
        if page > 0:
            nav_row.append(InlineKeyboardButton(text="⬅️", callback_data=f"task:{task_id}:parent:{page-1}"))
        if (page + 1) * page_size < total:
            nav_row.append(InlineKeyboardButton(text="➡️", callback_data=f"task:{task_id}:parent:{page+1}"))
        if nav_row:
            kb.append(nav_row)
        kb.append([
            InlineKeyboardButton(text="⬅ Назад", callback_data=f"task:{task_id}"),
            InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home"),
        ])
        return await _render_task_overlay(
            callback.message,
            db_pool,
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
        )

    if action == "parent_set" and len(parts) >= 4:
        parent_id = int(parts[3])
        if parent_id == task_id:
            return await _render_task_overlay(
                callback.message,
                db_pool,
                "⚠️ Нельзя назначить задачу родителем самой себе.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅ Назад", callback_data=f"task:{task_id}")]]),
            )

        async with db_pool.acquire() as conn:
            cur = await conn.fetchrow("SELECT project_id, kind FROM tasks WHERE id=$1", int(task_id))
            if not cur:
                return await _render_task_overlay(callback.message, db_pool, "❌ Задача не найдена.")
            if (cur.get("kind") or "task") == "super":
                await callback.answer("Суперзадача не может быть дочерней", show_alert=True)
                return await show_super_task_card(callback.message, db_pool, task_id, deps=deps, page=0)

            parent = await conn.fetchrow("SELECT id, project_id, kind FROM tasks WHERE id=$1", int(parent_id))
            if not parent:
                return await _render_task_overlay(callback.message, db_pool, "❌ Суперзадача не найдена.")
            if int(parent["project_id"]) != int(cur["project_id"]):
                return await _render_task_overlay(callback.message, db_pool, "⚠️ Нельзя назначить родителя из другого проекта.")
            if (parent.get("kind") or "task") != "super":
                return await _render_task_overlay(callback.message, db_pool, "⚠️ Родителем может быть только суперзадача.")

            info = await conn.fetchrow(
                "SELECT t.project_id, p.code as project_code, t.title FROM tasks t JOIN projects p ON p.id=t.project_id WHERE t.id=$1",
                task_id,
            )
            await conn.execute("UPDATE tasks SET parent_task_id=$2 WHERE id=$1", task_id, parent_id)
            if info:
                await db_add_event(conn, "task_parent_set", int(info["project_id"]), task_id, f"🔗 Привязано к суперзадаче #{parent_id} | [{info['project_code']}] #{task_id} {info['title']}")

        fire_and_forget(
            background_project_sync(int(cur["project_id"]), db_pool, vault, error_logger=lambda w, e, c: db_log_error(db_pool, w, e, c)),
            label="vault_sync",
        )
        return await show_task_card(callback.message, db_pool, task_id, deps=deps)

    # -----------------
    # Deadline controls
    # -----------------
    if action == "dl":
        return await _render_task_overlay(
            callback.message,
            db_pool,
            "🗓 Выберите срок:",
            reply_markup=task_deadline_kb(task_id),
        )

    if action == "dlset" and len(parts) >= 4:
        kind = parts[3]
        tz = _tz_from_deps(deps)
        now_local = datetime.now(tz)
        deadline_local: datetime | None = None

        if kind == "today":
            deadline_local = now_local.replace(hour=18, minute=0, second=0, microsecond=0)
        elif kind == "tomorrow":
            deadline_local = (now_local + timedelta(days=1)).replace(hour=18, minute=0, second=0, microsecond=0)
        elif kind == "+3":
            deadline_local = (now_local + timedelta(days=3)).replace(hour=18, minute=0, second=0, microsecond=0)
        elif kind == "+7":
            deadline_local = (now_local + timedelta(days=7)).replace(hour=18, minute=0, second=0, microsecond=0)
        elif kind == "none":
            deadline_local = None
        elif kind == "manual":
            await state.update_data(
                edit_task_id=task_id,
                wizard_chat_id=int(callback.message.chat.id),
                wizard_msg_id=int(callback.message.message_id),
            )
            # State itself is defined in bot.fsm.states
            from bot.fsm import EditTaskDeadline

            await state.set_state(EditTaskDeadline.entering)
            return await _render_task_overlay(
                callback.message,
                db_pool,
                "Введите дату/время (например 26.02 14:00 или 26.02).",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[
                        InlineKeyboardButton(text="⬅ Назад", callback_data=f"task:{task_id}:dl"),
                        InlineKeyboardButton(text="✖️ Отмена", callback_data=f"task:{task_id}:dlcancel"),
                    ]]
                ),
            )

        deadline_db = None
        if deadline_local is not None:
            deadline_db = to_db_utc(
                deadline_local,
                tz_name=deps.tz_name,
                store_tz=bool(getattr(deps, 'db_tasks_deadline_timestamptz', False)),
            )

        async with db_pool.acquire() as conn:
            info = await conn.fetchrow(
                "SELECT t.project_id, p.code as project_code, t.title FROM tasks t JOIN projects p ON p.id=t.project_id WHERE t.id=$1",
                task_id,
            )
            pid = int(info["project_id"]) if info else None
            await conn.execute("UPDATE tasks SET deadline=$2 WHERE id=$1", task_id, deadline_db)
            dl_txt = "без срока" if deadline_local is None else deadline_local.strftime("%d.%m %H:%M")
            if info:
                await db_add_event(conn, "task_deadline_changed", pid, task_id, f"🗓 Срок → {dl_txt} | [{info['project_code']}] #{task_id} {info['title']}")

        if pid:
            fire_and_forget(
                background_project_sync(int(pid), db_pool, vault, error_logger=lambda w, e, c: db_log_error(db_pool, w, e, c)),
                label="vault_sync",
            )
        return await show_task_card(callback.message, db_pool, task_id, deps=deps)

    # Fallback: reopen card
    return await show_task_card(callback.message, db_pool, task_id, deps=deps)


async def msg_edit_task_deadline(message: Message, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if deps.admin_id and (not message.from_user or message.from_user.id != deps.admin_id):
        return

    vault = deps.vault

    data = await state.get_data()
    task_id = data.get("edit_task_id")
    wiz_chat_id = int(data.get("wizard_chat_id") or message.chat.id)
    wiz_msg_id = data.get("wizard_msg_id")

    await try_delete_user_message(message)

    async def _point_ui_to_wizard() -> None:
        if wiz_msg_id:
            async with db_pool.acquire() as conn:
                await ui_set_state(conn, wiz_chat_id, ui_message_id=int(wiz_msg_id))

    if not task_id:
        await state.clear()
        await _point_ui_to_wizard()
        await ui_render(
            bot=message.bot,
            db_pool=db_pool,
            chat_id=wiz_chat_id,
            text="⚠️ Не удалось определить задачу.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home")]]
            ),
            screen=None,
            payload=None,
            fallback_message=None,
            parse_mode="HTML",
        )
        return

    parsed = await asyncio_to_thread_parse(message.text or "", deps.tz_name)
    if not parsed:
        await _point_ui_to_wizard()
        await ui_render(
            bot=message.bot,
            db_pool=db_pool,
            chat_id=wiz_chat_id,
            text="Не понял дату. Пример: 26.02 14:00 или 26.02.\n\nВведите дату/время (например 26.02 14:00 или 26.02).",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[
                    InlineKeyboardButton(text="⬅ Назад", callback_data=f"task:{int(task_id)}:dl"),
                    InlineKeyboardButton(text="✖️ Отмена", callback_data=f"task:{int(task_id)}:dlcancel"),
                ]]
            ),
            screen=None,
            payload=None,
            fallback_message=None,
            parse_mode=None,
        )
        return

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_tz_from_deps(deps))

    deadline_db = to_db_utc(
        parsed,
        tz_name=deps.tz_name,
        store_tz=bool(getattr(deps, 'db_tasks_deadline_timestamptz', False)),
    )
    async with db_pool.acquire() as conn:
        info = await conn.fetchrow(
            "SELECT t.project_id, p.code as project_code, t.title FROM tasks t JOIN projects p ON p.id=t.project_id WHERE t.id=$1",
            int(task_id),
        )
        pid = int(info["project_id"]) if info else None
        await conn.execute("UPDATE tasks SET deadline=$2 WHERE id=$1", int(task_id), deadline_db)
        if info:
            dl_txt = parsed.astimezone(_tz_from_deps(deps)).strftime("%d.%m %H:%M")
            await db_add_event(conn, "task_deadline_changed", pid, int(task_id), f"🗓 Срок → {dl_txt} | [{info['project_code']}] #{task_id} {info['title']}")

    if pid:
        fire_and_forget(
            background_project_sync(int(pid), db_pool, vault, error_logger=lambda w, e, c: db_log_error(db_pool, w, e, c)),
            label="vault_sync",
        )

    await _point_ui_to_wizard()
    await state.clear()
    return await show_task_card(message, db_pool, int(task_id), deps=deps)


async def asyncio_to_thread_parse(text: str, tz_name: str) -> datetime | None:
    # Keep the parsing in a thread to avoid blocking the event loop.
    import asyncio

    return await asyncio.to_thread(parse_datetime_ru, text, tz_name, prefer_future=True)


def register(dp: Dispatcher) -> None:
    dp.callback_query.register(cb_undo_task, F.data.startswith("undo:task:"))
    dp.callback_query.register(cb_task, F.data.startswith("task:"))
    from bot.fsm import EditTaskDeadline

    dp.message.register(msg_edit_task_deadline, StateFilter(EditTaskDeadline.entering), F.text)
