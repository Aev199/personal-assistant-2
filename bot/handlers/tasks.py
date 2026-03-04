"""Task-related handlers (task card drill-down).

This module is the next step after extracting navigation + projects.
It moves the task-card drill-down out of the monolith while preserving
existing callback_data format.
"""

from __future__ import annotations

import os
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
from bot.ui.state import ui_get_state, ui_set_state, _ui_payload_get, _undo_active, _now_ts
from bot.ui.task_card import task_card_kb, task_deadline_kb
from bot.tz import to_db_utc
from bot.utils import h, safe_edit, kb_columns





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


async def _guard(callback: CallbackQuery, deps: AppDeps) -> bool:
    if deps.admin_id and callback.from_user and callback.from_user.id != deps.admin_id:
        await callback.answer("Недоступно", show_alert=True)
        return False
    return True


async def show_task_card(msg: Message, db_pool: asyncpg.Pool, task_id: int, deps: AppDeps, *, expanded: bool = False) -> None:
    """Render task card into the SPA message."""
    tz = _tz_from_deps(deps)
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT t.id, t.title, t.status, t.deadline, t.parent_task_id,
                   t.g_task_id, t.g_task_list_id,
                   p.id AS project_id, p.code AS project_code,
                   COALESCE(tm.name, '—') AS assignee
            FROM tasks t
            JOIN projects p ON p.id = t.project_id
            LEFT JOIN team tm ON tm.id = t.assignee_id
            WHERE t.id=$1
            """,
            task_id,
        )
        if not row:
            await safe_edit(msg, "❌ Задача не найдена.")
            return

        subs = await conn.fetch(
            "SELECT id, title, status FROM tasks WHERE parent_task_id=$1 ORDER BY id",
            task_id,
        )

        ui_state = await ui_get_state(conn, int(msg.chat.id))
        payload = _ui_payload_get(ui_state)
        undo = _undo_active(payload, task_id=task_id)

    # Convert stored naive-UTC deadline to local time for display.
    dl = fmt_local(row["deadline"], tz)
    if getattr(deps, "logger", None):
        try:
            raw = row["deadline"]
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
    status = (row["status"] or "todo").lower()
    status_map = {
        "todo": "к выполнению",
        "in_progress": "в работе",
        "postponed": "отложено",
        "blocked": "отложено",
        "done": "готово",
    }
    st = status_map.get(status, status)

    lines = [
        f"🧩 <b>ЗАДАЧА</b> #{int(row['id'])}",
        f"Проект: <b>{h(str(row['project_code']))}</b>",
        f"Исполнитель: <b>{h(str(row['assignee']))}</b>",
        f"Статус: <b>{h(str(st))}</b>",
        f"Дедлайн: <b>{h(str(dl))}</b>",
    ]

    if undo:
        left = max(0, int(undo.get("exp", 0)) - _now_ts())
        lines.append(f"↩️ <i>Можно отменить последнее действие</i> (<b>{left}</b> сек)")

    lines += ["", f"Текст: {h(str(row['title']))}"]

    if row["parent_task_id"]:
        lines.append(f"Родитель: #{int(row['parent_task_id'])}")

    if subs:
        lines.append("")
        lines.append("<b>Подзадачи:</b>")
        for s in subs[:10]:
            mark = "✅" if s["status"] == "done" else "•"
            lines.append(f"{mark} #{int(s['id'])} {h(str(s['title']))}")
        if len(subs) > 10:
            lines.append(f"…и ещё {len(subs)-10}")

    active_subs = [(int(s["id"]), str(s["title"])) for s in (subs or []) if (s.get("status") != "done")]
    kb = task_card_kb(
        int(task_id),
        int(row["project_id"]),
        int(row["parent_task_id"]) if row["parent_task_id"] else None,
        str(status),
        in_gtasks=bool(row.get("g_task_id")),
        expanded=expanded,
        subtasks=active_subs,
    )

    if undo:
        kb.inline_keyboard.insert(0, [InlineKeyboardButton(text="↩️ Undo", callback_data=f"undo:task:{task_id}")])

    await safe_edit(msg, "\n".join(lines), reply_markup=kb, parse_mode="HTML")


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
                await safe_edit(callback.message, "❌ Задача не найдена.", parse_mode="HTML")
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

    # -----------------
    # Inbox triage: move task to another project
    # -----------------
    if action == "move":
        async with db_pool.acquire() as conn:
            info = await conn.fetchrow("SELECT t.project_id FROM tasks t WHERE t.id=$1", task_id)
            if not info:
                return await safe_edit(callback.message, "❌ Задача не найдена.")
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
        return await safe_edit(
            callback.message,
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
                return await safe_edit(callback.message, "❌ Задача не найдена.")
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
                await conn.execute("UPDATE tasks SET g_task_id=NULL, g_task_list_id=NULL WHERE id=$1", task_id)

        async def _forget_list_mapping(name: str) -> None:
            async with db_pool.acquire() as conn:
                await conn.execute("DELETE FROM g_tasks_lists WHERE name=$1", name)

        async def _create_task_with_list_retry(list_name: str, list_id: str, title: str, notes: str, due_utc) -> str | None:
            cur_list_id = list_id
            for attempt in range(2):
                try:
                    created = await gtasks.create_task(cur_list_id, title, notes=notes, due=due_utc)
                    gtid = created.get("id")
                    if gtid:
                        async with db_pool.acquire() as conn:
                            await conn.execute(
                                "UPDATE tasks SET g_task_id=$2, g_task_list_id=$3 WHERE id=$1",
                                task_id,
                                str(gtid),
                                str(cur_list_id),
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
                    SELECT t.id, t.title, t.deadline, t.status, t.g_task_id, t.g_task_list_id,
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

            list_name = (row["project_code"] or "").strip() or os.getenv("GTASKS_WORK_FALLBACK_LIST", "Работа")
            list_id = await get_or_create_list_id(db_pool, gtasks, list_name)

            title = f"[{row['project_code']}] #{task_id} {row['title']}" if row["project_code"] else f"#{task_id} {row['title']}"
            dl = fmt_local(row["deadline"], tz) if row["deadline"] else "—"
            notes = f"Проект: {row['project_code']}\nИсполнитель: {row['assignee']}\nДедлайн: {dl}\nID: {task_id}"

            due_utc = None
            if row["deadline"]:
                due_utc = due_from_local_date(to_local(row["deadline"], tz), tz)

            if row["g_task_id"] and row["g_task_list_id"]:
                try:
                    await gtasks.patch_task(str(row["g_task_list_id"]), str(row["g_task_id"]), title=title, notes=notes, due=due_utc)
                    await callback.answer("✅ Google Tasks: обновлено")
                except Exception as e:
                    # Common case: task was deleted manually from Google Tasks.
                    if _is_not_found(e):
                        await _clear_task_mapping()
                        gtid = await _create_task_with_list_retry(list_name, list_id, title, notes, due_utc)
                        if gtid:
                            await callback.answer("✅ Google Tasks: отправлено заново")
                        else:
                            await callback.answer("✅ Google Tasks: отправлено")
                    else:
                        raise
            else:
                gtid = await _create_task_with_list_retry(list_name, list_id, title, notes, due_utc)
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
                return await safe_edit(callback.message, "❌ Задача не найдена.")
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
        return await show_task_card(callback.message, db_pool, task_id, deps=deps)

    # -----------------
    # Assignee
    # -----------------
    if action == "assignee":
        async with db_pool.acquire() as conn:
            pid = await conn.fetchval("SELECT project_id FROM tasks WHERE id=$1", task_id)
            if not pid:
                return await safe_edit(callback.message, "❌ Задача не найдена.")
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
        return await safe_edit(callback.message, "👤 Выберите исполнителя:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

    if action == "assignee_set" and len(parts) >= 4:
        token = parts[3]
        new_assignee = None if token == "none" else int(token)
        async with db_pool.acquire() as conn:
            info = await conn.fetchrow(
                "SELECT t.project_id, p.code as project_code, t.title FROM tasks t JOIN projects p ON p.id=t.project_id WHERE t.id=$1",
                task_id,
            )
            if not info:
                return await safe_edit(callback.message, "❌ Задача не найдена.")
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
                return await safe_edit(callback.message, "❌ Задача не найдена.")
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
                return await safe_edit(callback.message, "❌ Задача не найдена.")
            project_id = row["project_id"]
            all_rows = await conn.fetch(
                "SELECT id, title, parent_task_id FROM tasks WHERE project_id=$1 AND status != 'done' ORDER BY id",
                project_id,
            )

        children: dict[int | None, list[int]] = {}
        title_by_id: dict[int, str] = {}
        for r in all_rows:
            tid = int(r["id"])
            pid = r["parent_task_id"]
            title_by_id[tid] = (r["title"] or "").strip()
            children.setdefault(pid, []).append(tid)

        def collect_descendants(root: int) -> set[int]:
            res: set[int] = set()
            stack = [root]
            while stack:
                cur = stack.pop()
                for c in children.get(cur, []):
                    if c not in res:
                        res.add(c)
                        stack.append(c)
            return res

        excluded = {task_id} | collect_descendants(task_id)
        candidates = [tid for tid in title_by_id.keys() if tid not in excluded]
        candidates.sort()

        total = len(candidates)
        chunk = candidates[page * page_size : (page + 1) * page_size]

        def _short(s: str, n: int = 36) -> str:
            return s if len(s) <= n else (s[: n - 1] + "…")

        lines = ["🔗 Выберите родительскую задачу:"]
        if not chunk:
            lines.append("Нет подходящих родительских задач.")
        kb = []
        for tid in chunk:
            kb.append([
                InlineKeyboardButton(text=f"[{tid}] {_short(title_by_id[tid], 30)}", callback_data=f"task:{task_id}:parent_set:{tid}")
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
        return await safe_edit(callback.message, "\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

    if action == "parent_set" and len(parts) >= 4:
        parent_id = int(parts[3])
        if parent_id == task_id:
            return await safe_edit(
                callback.message,
                "⚠️ Нельзя назначить задачу родителем самой себе.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅ Назад", callback_data=f"task:{task_id}")]]),
            )

        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT project_id FROM tasks WHERE id=$1", task_id)
            if not row:
                return await safe_edit(callback.message, "❌ Задача не найдена.")
            pid = row["project_id"]
            rels = await conn.fetch(
                "SELECT id, parent_task_id FROM tasks WHERE project_id=$1 AND status != 'done'",
                pid,
            )

        children: dict[int | None, list[int]] = {}
        for r in rels:
            children.setdefault(r["parent_task_id"], []).append(int(r["id"]))

        def collect_descendants(root: int) -> set[int]:
            res: set[int] = set()
            stack = [root]
            while stack:
                cur = stack.pop()
                for c in children.get(cur, []):
                    if c not in res:
                        res.add(c)
                        stack.append(c)
            return res

        if parent_id in collect_descendants(task_id):
            return await safe_edit(callback.message, "⚠️ Нельзя выбрать дочернюю задачу в качестве родителя (получится цикл).")

        async with db_pool.acquire() as conn:
            info = await conn.fetchrow(
                "SELECT t.project_id, p.code as project_code, t.title FROM tasks t JOIN projects p ON p.id=t.project_id WHERE t.id=$1",
                task_id,
            )
            await conn.execute("UPDATE tasks SET parent_task_id=$2 WHERE id=$1", task_id, parent_id)
            if info:
                await db_add_event(conn, "task_parent_set", int(info["project_id"]), task_id, f"🔗 Сделано подзадачей #{parent_id} | [{info['project_code']}] #{task_id} {info['title']}")

        fire_and_forget(
            background_project_sync(int(pid), db_pool, vault, error_logger=lambda w, e, c: db_log_error(db_pool, w, e, c)),
            label="vault_sync",
        )
        return await show_task_card(callback.message, db_pool, task_id, deps=deps)

    # -----------------
    # Deadline controls
    # -----------------
    if action == "dl":
        return await safe_edit(callback.message, "🗓 Выберите срок:", reply_markup=task_deadline_kb(task_id))

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
            await state.update_data(edit_task_id=task_id)
            # State itself is defined in bot.fsm.states
            from bot.fsm import EditTaskDeadline

            await state.set_state(EditTaskDeadline.entering)
            return await safe_edit(
                callback.message,
                "Введите дату/время (например 26.02 14:00 или 26.02).",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data="add:cancel")]]),
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
    if not task_id:
        await state.clear()
        return await message.answer("⚠️ Не удалось определить задачу.")

    parsed = await asyncio_to_thread_parse(message.text or "", deps.tz_name)
    if not parsed:
        return await message.answer("Не понял дату. Пример: 26.02 14:00 или 26.02.")

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
    await state.clear()
    await message.answer("✅ Срок обновлён.")
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
