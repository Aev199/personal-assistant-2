"""Project-related handlers (portfolio drill-down, linking, archiving)."""

from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from bot.tz import resolve_tzinfo

import asyncpg
from aiogram import Dispatcher, F
from aiogram.filters import StateFilter
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.fsm.context import FSMContext

from bot.fsm import AddProjectWizard
from bot.handlers.common import escape_hatch_menu_or_command
from bot.db import db_add_event, get_current_project_id, set_current_project_id
from bot.services.background import fire_and_forget
from bot.services.vault_sync import background_project_sync
from bot.deps import AppDeps

from bot.ui import ui_render, ui_render_projects_portfolio
from bot.ui.render import ui_safe_edit as safe_edit
from bot.ui.state import ui_get_state, ui_set_state, _ui_payload_get, ui_payload_with_toast
from bot.ui.task_tree import render_task_tree
from bot.utils import h, fmt_task_line_html, try_delete_user_message
from bot.keyboards import back_home_kb





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
    if d is None:
        return None
    return d.astimezone(tz)


def fmt_local(dt: datetime | None, tz: ZoneInfo) -> str:
    d = to_local(dt, tz)
    return d.strftime("%d.%m %H:%M") if d else "—"


async def _guard(callback: CallbackQuery, deps: AppDeps) -> bool:
    if deps.admin_id and callback.from_user and callback.from_user.id != deps.admin_id:
        await callback.answer("Недоступно", show_alert=True)
        return False
    return True


async def cb_proj_add_start(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if not await _guard(callback, deps):
        return
    await callback.answer()
    await state.clear()

    await state.set_state(AddProjectWizard.entering_data)
    await ui_render(
        bot=callback.bot,
        db_pool=db_pool,
        chat_id=int(callback.message.chat.id),
        text=(
            "🔗 <b>Связать проект</b>\n\n"
            "Введите <b>КОД</b> и <b>Имя файла</b> (без .md).\n"
            "Можно через пробел или через дефис.\n\n"
            "<i>Примеры:</i>\n"
            "• K-17 Редизайн сайта\n"
            "• K-17 — Редизайн сайта"
        ),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data="add:cancel")]]),
        screen="proj_add",
        payload={"mode": "link"},
        fallback_message=callback.message,
        parse_mode="HTML",
    )


async def msg_proj_add_data(message: Message, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    """SPA-мастер связывания проекта с Obsidian.

    Поведение соответствует старой команде "Свяжи проект":
    - принимает "K-17 Имя файла" или "K-17 — Имя файла"
    - если проект уже существует — обновляет имя файла
    - запускает синхронизацию в Vault/Obsidian
    """

    if deps.admin_id and (not message.from_user or message.from_user.id != deps.admin_id):
        return

    if await escape_hatch_menu_or_command(message, state, db_pool):
        return

    await try_delete_user_message(message)

    raw = (message.text or "").strip()

    # CODE <space> FILE or CODE — FILE
    if re.search(r"\s[-—]\s", raw):
        parts = re.split(r"\s*[-—]\s*", raw, maxsplit=1)
    else:
        parts = raw.split(maxsplit=1)

    code = (parts[0].strip() if parts else "").upper()
    name = (parts[1].strip() if len(parts) > 1 else "")
    name = name.replace(".md", "").strip()

    if not code or not name:
        await ui_render(
            bot=message.bot,
            db_pool=db_pool,
            chat_id=int(message.chat.id),
            text=(
                "⚠️ Введите в формате: <b>КОД</b> <b>Имя файла</b>\n\n"
                "<i>Примеры:</i>\n"
                "• K-17 Редизайн сайта\n"
                "• K-17 — Редизайн сайта"
            ),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data="add:cancel")]]),
            screen="proj_add",
            payload={"mode": "link"},
            fallback_message=None,
            parse_mode="HTML",
        )
        return

    try:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT id FROM projects WHERE code=$1", code)
            created = False
            if not row:
                project_id = await conn.fetchval(
                    "INSERT INTO projects (code, name, status) VALUES ($1, $2, 'active') RETURNING id",
                    code,
                    name,
                )
                created = True
                await db_add_event(conn, "project_created", int(project_id), None, f"🆕 Создан проект [{code}] {name}")
            else:
                project_id = int(row["id"])
                await conn.execute("UPDATE projects SET name=$1 WHERE id=$2", name, project_id)
                await db_add_event(conn, "project_linked", int(project_id), None, f"🔗 Проект [{code}] привязан: {name}.md")

            ui_state = await ui_get_state(conn, int(message.chat.id))
            payload = _ui_payload_get(ui_state)
            payload = ui_payload_with_toast(
                payload,
                f"✅ Проект <b>{h(code)}</b> " + ("создан и " if created else "") + f"связан с файлом <b>{h(name)}</b>.md",
                ttl_sec=20,
            )

        # Trigger sync
        fire_and_forget(
            background_project_sync(
                int(project_id),
                db_pool,
                vault,
                error_logger=deps.db_log_error,
            ),
            label=f"sync:proj:{int(project_id)}",
        )

        await state.clear()

        # Back to projects portfolio
        await ui_render_projects_portfolio(message, db_pool, tz_name=deps.tz_name, force_new=False)

    except Exception as e:
        await ui_render(
            bot=message.bot,
            db_pool=db_pool,
            chat_id=int(message.chat.id),
            text=f"❌ Ошибка загрузки. Для фикса: {h(str(e))}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data="add:cancel")]]),
            screen="proj_add",
            payload={"mode": "link"},
            fallback_message=None,
            parse_mode="HTML",
        )


async def cb_project_open(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    """Project drill-down navigation.

    - proj:{id} opens project card with task tree + root task buttons
    - proj:{id}:open:{page} paginates root tasks
    - proj:{id}:toggle_current toggles current project and returns to card
    - proj:{id}:tails / tails_pick:* keep project tails
    """

    if not await _guard(callback, deps):
        return

    await callback.answer()
    await state.clear()

    parts = (callback.data or "").split(":")
    if len(parts) < 2 or not parts[1].isdigit():
        return

    project_id = int(parts[1])
    action = parts[2] if len(parts) >= 3 else "open"
    if action == "status":
        action = "open"

    page = 0
    if len(parts) >= 4 and parts[3].isdigit():
        page = max(0, int(parts[3]))

    tz = _tz_from_deps(deps)

    try:
        async with db_pool.acquire() as conn:
            proj = await conn.fetchrow("SELECT id, code, name FROM projects WHERE id=$1", project_id)
            if not proj:
                await safe_edit(callback.message, "❌ Проект не найден.", reply_markup=back_home_kb(), parse_mode="HTML")
                return

            current_id = await get_current_project_id(conn, int(callback.message.chat.id))

            if action == "toggle_current":
                new_val = None if (current_id == project_id) else project_id
                await set_current_project_id(conn, int(callback.message.chat.id), new_val)
                current_id = new_val
                action = "open"
                page = 0

            # --- Archive project
            if action == "archive_ask":
                code = str(proj.get("code") or "").upper()
                if code == "INBOX":
                    await callback.answer("Inbox нельзя архивировать", show_alert=True)
                    action = "open"
                else:
                    confirm_text = "\n".join([
                        f"📦 <b>В архив — {h(code)}</b>",
                        "",
                        "Это действие:",
                        "• пометит проект как закрытый",
                        "• закроет все активные задачи",
                        "• перенесёт заметку в Obsidian/Архив (при следующей синхронизации)",
                        "",
                        "Продолжить?",
                    ])
                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="📦 Да, в архив", callback_data=f"proj:{project_id}:archive_do")],
                        [
                            InlineKeyboardButton(text="⬅ Назад", callback_data=f"proj:{project_id}:more:{page}"),
                            InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home"),
                        ],
                    ])
                    await ui_render(
                        bot=callback.bot,
                        db_pool=db_pool,
                        chat_id=int(callback.message.chat.id),
                        text=confirm_text,
                        reply_markup=kb,
                        screen="project_archive_confirm",
                        payload={"project_id": project_id},
                        fallback_message=callback.message,
                        parse_mode="HTML",
                    )
                    return

            if action == "archive_do":
                code = str(proj.get("code") or "").upper()
                if code == "INBOX":
                    await callback.answer("Inbox нельзя архивировать", show_alert=True)
                    action = "open"
                else:
                    await conn.execute("UPDATE projects SET status='done' WHERE id=$1", project_id)
                    await conn.execute("UPDATE tasks SET status='done' WHERE project_id=$1 AND status!='done'", project_id)
                    try:
                        await db_add_event(conn, "project_archived", project_id, None, f"📦 Проект {code} отправлен в архив")
                    except Exception:
                        pass

                    if current_id == project_id:
                        await set_current_project_id(conn, int(callback.message.chat.id), None)
                        current_id = None

                    ui_state = await ui_get_state(conn, int(callback.message.chat.id))
                    payload = _ui_payload_get(ui_state)
                    payload = ui_payload_with_toast(payload, f"📦 Проект <b>{h(code)}</b> отправлен в архив", ttl_sec=25)
                    await ui_set_state(conn, int(callback.message.chat.id), ui_screen="project_archived", ui_payload=payload)

                    vault = deps.vault

                    fire_and_forget(
                        background_project_sync(project_id, db_pool, vault, error_logger=deps.db_log_error),
                        label=f"sync:archive:{code}",
                    )

                    done_text = "\n".join([
                        f"📦 <b>{h(code)}</b> — в архиве",
                        "",
                        "Синхронизацию запустил в фоне.",
                    ])
                    done_kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="📁 Проекты", callback_data="nav:projects")],
                        [
                            InlineKeyboardButton(text="🔄 Статус синхронизации", callback_data="sync:status"),
                            InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home"),
                        ],
                    ])
                    await ui_render(
                        bot=callback.bot,
                        db_pool=db_pool,
                        chat_id=int(callback.message.chat.id),
                        text=done_text,
                        reply_markup=done_kb,
                        screen="project_archived",
                        payload=payload,
                        fallback_message=callback.message,
                        parse_mode="HTML",
                    )
                    return

            if action == "more":
                code = str(proj.get("code") or "")
                name = (proj.get("name") or "").strip()
                is_cur = current_id is not None and int(current_id) == int(project_id)
                lines = [f"<b>⋯ {h(code)}</b>" + (f" — {h(name)}" if name else ""), "", "<i>Дополнительные действия проекта.</i>"]
                more_kb_rows: list[list[InlineKeyboardButton]] = [
                    [
                        InlineKeyboardButton(text="🗂 Структура", callback_data=f"proj:{project_id}:structure:{page}"),
                        InlineKeyboardButton(text="🧺 Хвосты", callback_data=f"proj:{project_id}:tails:{page}"),
                    ],
                    [
                        InlineKeyboardButton(
                            text=("Снять текущий проект" if is_cur else "Сделать текущим"),
                            callback_data=f"proj:{project_id}:toggle_current",
                        ),
                    ],
                ]
                if code.upper() != "INBOX":
                    more_kb_rows.insert(2, [InlineKeyboardButton(text="📦 В архив", callback_data=f"proj:{project_id}:archive_ask:{page}")])
                more_kb_rows.append([
                    InlineKeyboardButton(text="⬅ Проект", callback_data=f"proj:{project_id}:open:{page}"),
                    InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home"),
                ])
                await ui_render(
                    bot=callback.bot,
                    db_pool=db_pool,
                    chat_id=int(callback.message.chat.id),
                    text="\n".join(lines),
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=more_kb_rows),
                    screen="project_more",
                    payload={"project_id": project_id, "page": page},
                    fallback_message=callback.message,
                    parse_mode="HTML",
                )
                return

            # --- Project tails
            if action == "structure":
                records = await conn.fetch(
                    """
                    SELECT t.id, t.title, t.kind, COALESCE(tm.name,'—') as assignee, t.deadline, t.parent_task_id, t.status
                    FROM tasks t
                    LEFT JOIN team tm ON tm.id=t.assignee_id
                    WHERE t.project_id=$1 AND t.status != 'done'
                    ORDER BY COALESCE(t.parent_task_id, t.id), t.id
                    """,
                    project_id,
                )
                tasks = [
                    {
                        "id": r["id"],
                        "title": r["title"],
                        "kind": r.get("kind") or "task",
                        "assignee": r["assignee"],
                        "deadline": r["deadline"],
                        "parent_task_id": r["parent_task_id"],
                        "status": r["status"] or "todo",
                    }
                    for r in records
                ]
                tree_text = "✅ Задач нет." if not tasks else render_task_tree(tasks, tz)[0]
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="⬅ Ещё", callback_data=f"proj:{project_id}:more:{page}")],
                    [InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home")],
                ])
                await ui_render(
                    bot=callback.bot,
                    db_pool=db_pool,
                    chat_id=int(callback.message.chat.id),
                    text="\n".join([f"<b>🗂 Структура — {h(proj['code'])}</b>", "", tree_text]),
                    reply_markup=kb,
                    screen="project_structure",
                    payload={"project_id": project_id},
                    fallback_message=callback.message,
                    parse_mode="HTML",
                )
                return

            if action == "tails":
                nodate = await conn.fetchval(
                    "SELECT COUNT(*) FROM tasks WHERE project_id=$1 AND status != 'done' AND kind != 'super' AND deadline IS NULL",
                    project_id,
                )
                postponed = await conn.fetchval(
                    "SELECT COUNT(*) FROM tasks WHERE project_id=$1 AND kind != 'super' AND status='postponed' AND status != 'done'",
                    project_id,
                )
                text = "\n".join([
                    f"<b>🧺 ХВОСТЫ — {h(proj['code'])}</b>",
                    f"💤 Без срока: <b>{int(nodate or 0)}</b>",
                    f"⏸ Отложено: <b>{int(postponed or 0)}</b>",
                    "",
                    "<i>Выберите список:</i>",
                ])
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="💤 Без срока", callback_data=f"proj:{project_id}:tails_pick:nodate:{page}")],
                    [InlineKeyboardButton(text="⏸ Отложено", callback_data=f"proj:{project_id}:tails_pick:postponed:{page}")],
                    [
                        InlineKeyboardButton(text="⬅ Ещё", callback_data=f"proj:{project_id}:more:{page}"),
                        InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home"),
                    ],
                ])
                await ui_render(
                    bot=callback.bot,
                    db_pool=db_pool,
                    chat_id=int(callback.message.chat.id),
                    text=text,
                    reply_markup=kb,
                    screen="project_tails",
                    payload={"project_id": project_id, "view": "tails"},
                    fallback_message=callback.message,
                    parse_mode="HTML",
                )
                return

            if action == "tails_pick":
                kind = parts[3] if len(parts) >= 4 else "nodate"
                page = int(parts[4]) if len(parts) >= 5 and parts[4].isdigit() else 0
                page = max(0, page)
                page_size = 8
                where = "t.deadline IS NULL" if kind == "nodate" else "t.status='postponed'"

                total = await conn.fetchval(
                    f"SELECT COUNT(*) FROM tasks t WHERE t.project_id=$1 AND t.status != 'done' AND t.kind != 'super' AND {where}",
                    project_id,
                )
                rows = await conn.fetch(
                    f"""
                    SELECT t.id, t.title, COALESCE(tm.name,'—') AS assignee, t.deadline
                    FROM tasks t
                    LEFT JOIN team tm ON t.assignee_id = tm.id
                    WHERE t.project_id=$1 AND t.status != 'done' AND t.kind != 'super' AND {where}
                    ORDER BY COALESCE(t.deadline, TIMESTAMP '9999-12-31'), t.id
                    LIMIT $2 OFFSET $3
                    """,
                    project_id,
                    page_size,
                    page * page_size,
                )

                def _short(s: str, n: int = 30) -> str:
                    s = (s or "").strip()
                    return s if len(s) <= n else (s[: n - 1] + "…")

                def _tail_caption(r: dict) -> str:
                    dt_loc = to_local(r["deadline"], tz) if r.get("deadline") else None
                    assignee = str(r.get("assignee") or "—").strip()
                    meta: list[str] = []
                    if assignee and assignee != "—":
                        meta.append(assignee)
                    if dt_loc:
                        meta.append(dt_loc.strftime("%d.%m %H:%M"))
                    elif kind == "nodate":
                        meta.append("без срока")
                    else:
                        meta.append("отложено")
                    prefix = "💤" if kind == "nodate" else "⏸"
                    suffix = f" — {' • '.join(meta)}" if meta else ""
                    return f"{prefix} {_short(str(r.get('title') or ''), 26)}{suffix}"

                title = "💤 БЕЗ СРОКА" if kind == "nodate" else "⏸ ОТЛОЖЕНО"
                total_i = int(total or 0)
                lines = [f"<b>🧺 {h(title)} — {h(proj['code'])}</b>", f"<i>Всего: {total_i}</i>", ""]
                if not rows:
                    lines.append("Задач нет.")
                else:
                    lines.append("Нажмите на задачу ниже, чтобы открыть карточку.")

                kb_rows: list[list[InlineKeyboardButton]] = []
                for r in rows:
                    kb_rows.append([InlineKeyboardButton(text=_tail_caption(dict(r)), callback_data=f"task:{r['id']}")])

                nav_row: list[InlineKeyboardButton] = []
                if page > 0:
                    nav_row.append(InlineKeyboardButton(text="⬅️", callback_data=f"proj:{project_id}:tails_pick:{kind}:{page-1}"))
                if (page + 1) * page_size < int(total or 0):
                    nav_row.append(InlineKeyboardButton(text="➡️", callback_data=f"proj:{project_id}:tails_pick:{kind}:{page+1}"))
                if nav_row:
                    kb_rows.append(nav_row)

                kb_rows.append([
                    InlineKeyboardButton(text="⬅ Хвосты", callback_data=f"proj:{project_id}:tails:{page}"),
                    InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home"),
                ])
                kb_rows.append([InlineKeyboardButton(text="⬅ Ещё", callback_data=f"proj:{project_id}:more:{page}")])

                await ui_render(
                    bot=callback.bot,
                    db_pool=db_pool,
                    chat_id=int(callback.message.chat.id),
                    text="\n".join(lines),
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
                    screen="project_tails_pick",
                    payload={"project_id": project_id, "kind": kind, "page": page},
                    fallback_message=callback.message,
                    parse_mode="HTML",
                )
                return

            # --- Project card
            stats = await conn.fetchrow(
                """
                SELECT
                  COUNT(*) FILTER (WHERE status != 'done' AND kind != 'super') AS active,
                  COUNT(*) FILTER (WHERE status != 'done' AND kind != 'super' AND deadline IS NOT NULL AND deadline < (NOW() AT TIME ZONE 'UTC')) AS overdue
                FROM tasks WHERE project_id=$1
                """,
                project_id,
            )

            page_size = 8
            total_roots = await conn.fetchval(
                "SELECT COUNT(*) FROM tasks WHERE project_id=$1 AND parent_task_id IS NULL AND status != 'done'",
                project_id,
            )
            root_rows = await conn.fetch(
                """
                SELECT id, title, kind
                FROM tasks
                WHERE project_id=$1 AND parent_task_id IS NULL AND status != 'done'
                ORDER BY id
                LIMIT $2 OFFSET $3
                """,
                project_id,
                page_size,
                page * page_size,
            )

        def _short_btn(s: str, n: int = 30) -> str:
            s = (s or "").strip()
            return s if len(s) <= n else (s[: n - 1] + "…")

        root_btns = [
            [
                InlineKeyboardButton(
                    text=_short_btn(("🧩 " if (r.get("kind") == "super") else "") + str(r["title"]), 30),
                    callback_data=f"task:{int(r['id'])}",
                )
            ]
            for r in root_rows
        ]

        pager: list[InlineKeyboardButton] = []
        if page > 0:
            pager.append(InlineKeyboardButton(text="⬅️", callback_data=f"proj:{project_id}:open:{page-1}"))
        if (page + 1) * page_size < int(total_roots or 0):
            pager.append(InlineKeyboardButton(text="➡️", callback_data=f"proj:{project_id}:open:{page+1}"))
        if pager:
            root_btns.append(pager)

        code = proj.get("code") or ""
        name = (proj.get("name") or "").strip()
        is_cur = current_id is not None and int(current_id) == int(project_id)
        active = int(stats.get("active") or 0) if stats else 0
        overdue = int(stats.get("overdue") or 0) if stats else 0

        head = f"<b>📁 {h(str(code))}</b>" + (f" — {h(name)}" if name else "")
        meta_bits = [f"активных: <b>{active}</b>"]
        if overdue:
            meta_bits.append(f"🚨 <b>{overdue}</b>")
        if is_cur:
            meta_bits.append("⭐ <b>текущий</b>")

        lines = [head, "<i>" + " • ".join(meta_bits) + "</i>", "", "<i>Нажмите на корневую задачу ниже или откройте дополнительные действия.</i>"]

        kb: list[list[InlineKeyboardButton]] = []
        kb.extend(root_btns)

        # Actions
        kb.append([
            InlineKeyboardButton(text="➕ Задача", callback_data=f"add:task:{project_id}"),
            InlineKeyboardButton(text="🧩 Суперзадача", callback_data=f"add:super:{project_id}"),
        ])
        kb.append([InlineKeyboardButton(text="⋯ Ещё", callback_data=f"proj:{project_id}:more:{page}")])

        kb.append([
            InlineKeyboardButton(text="⬅️ Проекты", callback_data="nav:projects"),
            InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home"),
        ])

        await ui_render(
            bot=callback.bot,
            db_pool=db_pool,
            chat_id=int(callback.message.chat.id),
            text="\n".join(lines).strip(),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
            screen="project_card",
            payload={"project_id": project_id, "page": page},
            fallback_message=callback.message,
            parse_mode="HTML",
        )

    except Exception as e:
        await safe_edit(callback.message, f"❌ Ошибка загрузки. Для фикса: {h(str(e))}", reply_markup=back_home_kb(), parse_mode="HTML")


def register(dp: Dispatcher) -> None:
    dp.callback_query.register(cb_proj_add_start, F.data == "proj:add:start")
    dp.message.register(msg_proj_add_data, StateFilter(AddProjectWizard.entering_data), F.text)
    dp.callback_query.register(cb_project_open, F.data.startswith("proj:"))
