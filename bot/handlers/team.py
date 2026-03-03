"""Team handlers.

- Reply-menu entry: "Команда"
- Team member drill-down (team:<id>:<page>)
- Add team member wizard
"""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

import asyncpg
from aiogram import Dispatcher, F
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.deps import AppDeps

from bot.db import db_log_error
from bot.fsm import AddTeamWizard
from bot.handlers.common import escape_hatch_menu_or_command
from bot.ui import ui_render
from bot.ui.screens import ui_render_team
from bot.ui.state import ui_get_state, ui_set_state, _ui_payload_get, _now_ts
from bot.utils import canon, h, kb_columns, safe_edit, try_delete_user_message, wizard_render
from bot.keyboards import back_home_kb


UTC = ZoneInfo("UTC")



def to_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


async def cmd_team_load(message: Message, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if not message.from_user or message.from_user.id != deps.admin_id:
        return
    await state.clear()
    await try_delete_user_message(message)
    await ui_render_team(message, db_pool, force_new=True)


async def cb_team_add(callback: CallbackQuery, state: FSMContext, deps: AppDeps) -> None:
    if not callback.from_user or callback.from_user.id != deps.admin_id:
        return await callback.answer("Недоступно", show_alert=True)
    await callback.answer()
    await state.clear()

    await state.update_data(
        wizard_chat_id=int(callback.message.chat.id),
        wizard_msg_id=int(callback.message.message_id),
    )
    await state.set_state(AddTeamWizard.entering)
    await wizard_render(
        bot=callback.bot,
        state=state,
        chat_id=int(callback.message.chat.id),
        fallback_msg=callback.message,
        text="➕ <b>Новый сотрудник</b>\n\nОтправьте: <i>Имя — роль</i> (роль можно опустить).",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data="add:cancel")]]
        ),
        parse_mode="HTML",
    )


async def msg_team_add(message: Message, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if not message.from_user or message.from_user.id != deps.admin_id:
        return
    if await escape_hatch_menu_or_command(message, state, db_pool):
        return
    if not message.text:
        return

    raw = (message.text or "").strip()
    await try_delete_user_message(message)

    name, role = raw, ""
    try:
        parts = re.split(r"\s*[-—]\s*", raw, maxsplit=1)
        if parts:
            name = (parts[0] or "").strip()
            role = (parts[1] or "").strip() if len(parts) > 1 else ""
    except Exception:
        pass

    name = (name or "").strip()
    if not name:
        return await wizard_render(
            bot=message.bot,
            state=state,
            chat_id=int(message.chat.id),
            fallback_msg=None,
            text="⚠️ Введите имя. Пример: <i>Олег — инженер</i>",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data="add:cancel")]]
            ),
            parse_mode="HTML",
        )

    try:
        async with db_pool.acquire() as conn:
            try:
                await conn.execute("INSERT INTO team(name, role) VALUES($1,$2)", name, role)
            except Exception:
                # If unique constraint exists on name — update role.
                try:
                    await conn.execute("UPDATE team SET role=$2 WHERE name=$1", name, role)
                except Exception:
                    raise

            ui_state = await ui_get_state(conn, int(message.chat.id))
            payload = _ui_payload_get(ui_state)
            payload["toast"] = {"text": f"✅ Сотрудник <b>{h(name)}</b> добавлен", "exp": _now_ts() + 20}
            await ui_set_state(conn, int(message.chat.id), ui_payload=payload)

        await state.clear()
        await ui_render_team(message, db_pool, force_new=False)
    except Exception as e:
        await db_log_error(db_pool, "team_add", e, {"name": name, "role": role})
        await wizard_render(
            bot=message.bot,
            state=state,
            chat_id=int(message.chat.id),
            fallback_msg=None,
            text=f"❌ Ошибка: {h(str(e))}",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data="add:cancel")]]
            ),
            parse_mode="HTML",
        )


async def cb_team_member_details(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if not callback.from_user or callback.from_user.id != deps.admin_id:
        return await callback.answer("Недоступно", show_alert=True)

    await callback.answer()
    await state.clear()

    parts = callback.data.split(":")
    if len(parts) < 2 or not parts[1].isdigit():
        return

    emp_id = int(parts[1])
    page = 0
    if len(parts) >= 3 and parts[2].isdigit():
        page = max(0, int(parts[2]))

    page_size = 8
    try:
        async with db_pool.acquire() as conn:
            tm = await conn.fetchrow("SELECT name, role FROM team WHERE id = $1", emp_id)
            if not tm:
                return await safe_edit(
                    callback.message,
                    "❌ Сотрудник не найден.",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home")]]
                    ),
                    parse_mode="HTML",
                )

            total_tasks = int(
                await conn.fetchval(
                    "SELECT COUNT(*) FROM tasks WHERE assignee_id = $1 AND status != 'done'",
                    emp_id,
                )
                or 0
            )
            overdue_count = int(
                await conn.fetchval(
                    "SELECT COUNT(*) FROM tasks WHERE assignee_id = $1 AND status != 'done' AND deadline IS NOT NULL AND deadline < (NOW() AT TIME ZONE 'UTC')",
                    emp_id,
                )
                or 0
            )

            pages = max(1, (total_tasks + page_size - 1) // page_size)
            if page >= pages:
                page = max(0, pages - 1)

            tasks = await conn.fetch(
                """
                SELECT t.id, t.title, p.code AS project_code, t.deadline
                FROM tasks t
                JOIN projects p ON t.project_id = p.id
                WHERE t.assignee_id = $1 AND t.status != 'done'
                ORDER BY (t.deadline IS NULL), t.deadline ASC NULLS LAST, p.code, t.id
                LIMIT $2 OFFSET $3
                """,
                emp_id,
                page_size,
                page * page_size,
            )

        name = str(tm["name"] or "")
        role = str(tm["role"] or "")

        lines: list[str] = []
        if role:
            lines.append(f"👤 <b>{h(name)}</b> — <i>{h(role)}</i>")
        else:
            lines.append(f"👤 <b>{h(name)}</b>")
        lines.append(f"📊 Активных задач: <b>{total_tasks}</b>")
        lines.append(f"🚨 Просрочено: <b>{overdue_count}</b>")

        kb: list[list[InlineKeyboardButton]] = []

        if not tasks:
            lines.append("")
            lines.append("✅ Сейчас нет активных задач.")
            kb.append([
                InlineKeyboardButton(text="⬅ Назад", callback_data="nav:team"),
                InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home"),
            ])
            return await ui_render(
                bot=callback.bot,
                db_pool=db_pool,
                chat_id=int(callback.message.chat.id),
                text="\n".join(lines).strip(),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
                screen="team_member",
                payload={"emp_id": emp_id, "page": page},
                fallback_message=callback.message,
                parse_mode="HTML",
            )

        lines.append("")
        lines.append("👇 Задачи в работе:")

        now_utc = datetime.now(UTC)
        for t in tasks:
            title = str(t["title"] or "")
            title_short = (title[:22] + "…") if len(title) > 25 else title
            project_code = str(t["project_code"] or "")
            marker = "📝"
            if t["deadline"]:
                try:
                    if (to_utc(t["deadline"]) or now_utc) < now_utc:
                        marker = "🚨"
                except Exception:
                    pass
            kb.append([
                InlineKeyboardButton(
                    text=f"{marker} [{project_code}] {title_short}",
                    callback_data=f"task:{int(t['id'])}",
                )
            ])

        if pages > 1:
            pager: list[InlineKeyboardButton] = []
            if page > 0:
                pager.append(InlineKeyboardButton(text="⬅️", callback_data=f"team:{emp_id}:{page-1}"))
            if page + 1 < pages:
                pager.append(InlineKeyboardButton(text="➡️", callback_data=f"team:{emp_id}:{page+1}"))
            if pager:
                kb.append(pager)

        kb.append([
            InlineKeyboardButton(text="⬅ Назад", callback_data="nav:team"),
            InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home"),
        ])

        await ui_render(
            bot=callback.bot,
            db_pool=db_pool,
            chat_id=int(callback.message.chat.id),
            text="\n".join(lines).strip(),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
            screen="team_member",
            payload={"emp_id": emp_id, "page": page},
            fallback_message=callback.message,
            parse_mode="HTML",
        )

    except Exception as e:
        await safe_edit(
            callback.message,
            f"❌ Ошибка: {h(str(e))}",
            reply_markup=back_home_kb(),
            parse_mode="HTML",
        )


def register(dp: Dispatcher) -> None:
    dp.message.register(cmd_team_load, lambda m: m.text and canon(m.text) == "команда")

    dp.callback_query.register(cb_team_add, F.data == "team:add")
    dp.message.register(msg_team_add, StateFilter(AddTeamWizard.entering), F.text)

    dp.callback_query.register(cb_team_member_details, F.data.startswith("team:"))
