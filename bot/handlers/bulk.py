"""Bulk actions for overdue tasks ("разгрести").

Keeps selection state per chat in memory; applies updates in one transaction.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from bot.tz import resolve_tz_name, to_db_utc

import asyncpg
from aiogram import Dispatcher, F
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.fsm.context import FSMContext

from bot.deps import AppDeps

from bot.db import db_add_event
from bot.ui.screens import ui_render_overdue, to_local
from bot.utils import h, safe_edit, kb_columns
from bot.keyboards import back_home_kb




UTC = ZoneInfo("UTC")


# In-memory sessions: chat_id -> {selected: set[int], page: int, total: int}
bulk_sessions: dict[int, dict] = {}


async def _fetch_overdue_page(conn: asyncpg.Connection, page: int, page_size: int):
    total = await conn.fetchval(
        "SELECT COUNT(*) FROM tasks WHERE kind != 'super' AND deadline IS NOT NULL AND deadline < (NOW() AT TIME ZONE 'UTC') AND status != 'done'"
    )
    rows = await conn.fetch(
        """
        SELECT t.id, t.title, p.code AS project, COALESCE(tm.name,'—') AS assignee, t.deadline
        FROM tasks t
        JOIN projects p ON t.project_id = p.id
        LEFT JOIN team tm ON t.assignee_id = tm.id
        WHERE t.kind != 'super' AND t.deadline IS NOT NULL AND t.deadline < (NOW() AT TIME ZONE 'UTC') AND t.status != 'done'
        ORDER BY t.deadline ASC
        LIMIT $1 OFFSET $2
        """,
        page_size,
        page * page_size,
    )
    return int(total or 0), list(rows or [])


def _bulk_action_label(action: str) -> str:
    return {
        "tomorrow10": "➡️ Завтра 10:00",
        "postpone": "⏸ Отложить",
        "unassign": "👤 Снять исполнителя",
        "done": "✅ Закрыть",
    }.get(action, action)


def _short(s: str, n: int = 26) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else (s[: n - 1] + "…")


async def _render_bulk(msg: Message, db_pool: asyncpg.Pool, chat_id: int, deps: AppDeps, page: int = 0) -> None:
    page = max(0, page)
    page_size = 8
    sess = bulk_sessions.get(chat_id) or {"selected": set(), "page": 0}
    selected: set[int] = set(sess.get("selected", set()))

    tz = ZoneInfo(deps.tz_name or "Europe/Moscow")

    async with db_pool.acquire() as conn:
        total, rows = await _fetch_overdue_page(conn, page, page_size)

    sess["page"] = page
    sess["total"] = total
    sess["selected"] = selected
    bulk_sessions[chat_id] = sess

    parts = ["<b>🧹 РАЗГРЕСТИ ПРОСРОЧКИ</b>", "<i>Выберите задачи:</i>", ""]
    if not rows:
        parts.append("Просроченных задач нет 🎉")
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅ Просрочки", callback_data="nav:overdue:0"), InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home")]]
        )
        await safe_edit(msg, "\n".join(parts), reply_markup=kb, parse_mode="HTML")
        return

    # Human-readable list in text, but keep within Telegram limits
    for r in rows:
        tid = int(r["id"])
        checked = "✅" if tid in selected else "☐"
        dt_loc = to_local(r.get("deadline"), tz)
        when = dt_loc.strftime("%d.%m %H:%M") if dt_loc else "—"
        parts.append(f"{h(checked)} <b>{h(_short(r.get('title') or '', 60))}</b>")
        parts.append("<i>" + h(" • ".join([(r.get("project") or ""), (r.get("assignee") or "—"), f"до {when}"])) + "</i>")
        parts.append("")

    # Compact 2-column selection buttons
    buttons: list[InlineKeyboardButton] = []
    for r in rows:
        tid = int(r["id"])
        checked = "✅" if tid in selected else "☐"
        label = f"{checked} {_short(str(r.get('title') or ''), 18)}"
        buttons.append(InlineKeyboardButton(text=label, callback_data=f"bulk:toggle:{tid}:{page}"))

    kb_rows: list[list[InlineKeyboardButton]] = []
    kb_rows.extend(kb_columns(buttons, 2))

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"bulk:page:{page-1}"))
    if (page + 1) * page_size < total:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"bulk:page:{page+1}"))
    if nav:
        kb_rows.append(nav)

    kb_rows.append([
        InlineKeyboardButton(text="➡️ Завтра 10:00", callback_data="bulk:act:tomorrow10"),
        InlineKeyboardButton(text="⏸ Отложить", callback_data="bulk:act:postpone"),
    ])
    kb_rows.append([
        InlineKeyboardButton(text="👤 Снять исп.", callback_data="bulk:act:unassign"),
        InlineKeyboardButton(text="✅ Закрыть", callback_data="bulk:act:done"),
    ])
    kb_rows.append([InlineKeyboardButton(text="✖️ Отмена", callback_data="bulk:cancel")])
    kb_rows.append([
        InlineKeyboardButton(text="⬅ Просрочки", callback_data="nav:overdue:0"),
        InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home"),
    ])

    await safe_edit(msg, "\n".join(parts).strip(), reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows), parse_mode="HTML")


async def cb_bulk(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if deps.admin_id and callback.from_user and callback.from_user.id != deps.admin_id:
        return await callback.answer("Недоступно", show_alert=True)

    await callback.answer()
    await state.clear()

    chat_id = int(callback.message.chat.id)
    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) >= 2 else ""

    try:
        if action == "start":
            page = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 0
            bulk_sessions[chat_id] = {"selected": set(), "page": page}
            return await _render_bulk(callback.message, db_pool, chat_id, deps, page=page)

        sess = bulk_sessions.get(chat_id) or {"selected": set(), "page": 0}

        if action == "toggle":
            tid = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else None
            page = int(parts[3]) if len(parts) >= 4 and parts[3].isdigit() else int(sess.get("page", 0) or 0)
            if tid is not None:
                sel: set[int] = set(sess.get("selected", set()))
                if tid in sel:
                    sel.remove(tid)
                else:
                    sel.add(tid)
                sess["selected"] = sel
                bulk_sessions[chat_id] = sess
            return await _render_bulk(callback.message, db_pool, chat_id, deps, page=page)

        if action == "page":
            page = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 0
            return await _render_bulk(callback.message, db_pool, chat_id, deps, page=page)

        if action == "cancel":
            bulk_sessions.pop(chat_id, None)
            return await ui_render_overdue(callback.message, db_pool, force_new=False)

        if action == "act":
            act = parts[2] if len(parts) >= 3 else ""
            sel: set[int] = set(sess.get("selected", set()))
            if not sel:
                return await callback.answer("Сначала выберите задачи", show_alert=True)

            text = f"Подтвердите действие: {_bulk_action_label(act)}\nЗадач: {len(sel)}"
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="✅ Да", callback_data=f"bulk:confirm:{act}")],
                    [InlineKeyboardButton(text="❌ Нет", callback_data=f"bulk:start:{int(sess.get('page',0) or 0)}")],
                ]
            )
            return await safe_edit(callback.message, text, reply_markup=kb)

        if action == "confirm":
            act = parts[2] if len(parts) >= 3 else ""
            sel: set[int] = set(sess.get("selected", set()))
            if not sel:
                return await callback.answer("Нечего применять", show_alert=True)

            async with db_pool.acquire() as conn:
                async with conn.transaction():
                    infos = await conn.fetch(
                        """
                        SELECT t.id, t.title, t.project_id, p.code AS project_code
                        FROM tasks t JOIN projects p ON t.project_id=p.id
                        WHERE t.id = ANY($1::bigint[]) AND t.kind != 'super'
                        """,
                        list(sel),
                    )
                    info_by_id = {int(r["id"]): r for r in infos}

                    if act == "tomorrow10":
                        now_local = datetime.now(ZoneInfo(deps.tz_name or "Europe/Moscow"))
                        d = (now_local + timedelta(days=1)).date()
                        dt_local = datetime(d.year, d.month, d.day, 10, 0, tzinfo=ZoneInfo(deps.tz_name or "Europe/Moscow"))
                        dl = to_db_utc(
                            dt_local,
                            tz_name=deps.tz_name,
                            store_tz=bool(getattr(deps, 'db_tasks_deadline_timestamptz', False)),
                        )
                        await conn.execute(
                            "UPDATE tasks SET deadline=$2, status='todo' WHERE id=ANY($1::bigint[]) AND kind != 'super' AND status!='done'",
                            list(sel),
                            dl,
                        )
                        for tid in sel:
                            inf = info_by_id.get(int(tid))
                            if inf:
                                await db_add_event(
                                    conn,
                                    "task_deadline_changed",
                                    int(inf["project_id"]),
                                    int(tid),
                                    f"🗓 Срок | [{inf['project_code']}] #{tid} → завтра 10:00",
                                )
                    elif act == "postpone":
                        await conn.execute(
                            "UPDATE tasks SET status='postponed' WHERE id=ANY($1::bigint[]) AND kind != 'super' AND status!='done'",
                            list(sel),
                        )
                        for tid in sel:
                            inf = info_by_id.get(int(tid))
                            if inf:
                                await db_add_event(
                                    conn,
                                    "task_postponed",
                                    int(inf["project_id"]),
                                    int(tid),
                                    f"⏸ Отложено | [{inf['project_code']}] #{tid} {inf['title']}",
                                )
                    elif act == "unassign":
                        await conn.execute(
                            "UPDATE tasks SET assignee_id=NULL WHERE id=ANY($1::bigint[]) AND kind != 'super' AND status!='done'",
                            list(sel),
                        )
                        for tid in sel:
                            inf = info_by_id.get(int(tid))
                            if inf:
                                await db_add_event(
                                    conn,
                                    "task_assignee_changed",
                                    int(inf["project_id"]),
                                    int(tid),
                                    f"👤 Исполнитель | [{inf['project_code']}] #{tid} → без исполнителя",
                                )
                    elif act == "done":
                        await conn.execute(
                            "UPDATE tasks SET status='done' WHERE id=ANY($1::bigint[]) AND kind != 'super' AND status!='done'",
                            list(sel),
                        )
                        for tid in sel:
                            inf = info_by_id.get(int(tid))
                            if inf:
                                await db_add_event(
                                    conn,
                                    "task_done",
                                    int(inf["project_id"]),
                                    int(tid),
                                    f"✅ Закрыто | [{inf['project_code']}] #{tid} {inf['title']}",
                                )
                    else:
                        return await callback.answer("Неизвестное действие", show_alert=True)

            bulk_sessions.pop(chat_id, None)
            return await ui_render_overdue(callback.message, db_pool, force_new=False)

    except Exception as e:
        await safe_edit(callback.message, f"❌ Ошибка: {h(str(e))}", reply_markup=back_home_kb(), parse_mode="HTML")


def register(dp: Dispatcher) -> None:
    dp.callback_query.register(cb_bulk, F.data.startswith("bulk:"))
