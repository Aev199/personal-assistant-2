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

from bot.db import db_log_error, get_persona_mode
from bot.fsm import AddTeamWizard, EditTeamNoteWizard
from bot.handlers.common import (
    cleanup_stale_wizard_message,
    escape_hatch_menu_or_command,
    get_wizard_message_data,
    split_wizard_message_target,
)
from bot.ui import ui_render
from bot.ui.render import ui_safe_edit as safe_edit, ui_safe_wizard_render as wizard_render
from bot.ui.screens import ui_render_team
from bot.ui.state import ui_get_state, ui_set_state, _ui_payload_get, ui_payload_with_toast
from bot.utils import canon, h, try_delete_user_message
from bot.keyboards import back_home_kb
from bot.persona import is_solo_mode


UTC = ZoneInfo("UTC")


def to_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


async def cmd_team_load(message: Message, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if deps.admin_id and (not message.from_user or message.from_user.id != deps.admin_id):
        return
    async with db_pool.acquire() as conn:
        persona_mode = await get_persona_mode(conn, int(message.chat.id))
    if is_solo_mode(persona_mode):
        await state.clear()
        await try_delete_user_message(message)
        return await ui_render_team(message, db_pool, force_new=False)
    wizard_chat_id, wizard_msg_id = await get_wizard_message_data(
        state,
        fallback_chat_id=int(message.chat.id),
    )
    preferred_message_id, stale_wizard_msg_id = split_wizard_message_target(
        wizard_msg_id,
        prefer_wizard=True,
    )
    await state.clear()
    await try_delete_user_message(message)
    final_id = await ui_render_team(
        message,
        db_pool,
        preferred_message_id=preferred_message_id,
        force_new=False,
    )
    await cleanup_stale_wizard_message(
        message.bot,
        chat_id=wizard_chat_id,
        stale_message_id=stale_wizard_msg_id,
        final_message_id=final_id,
    )


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
    if deps.admin_id and (not message.from_user or message.from_user.id != deps.admin_id):
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
            payload = ui_payload_with_toast(payload, f"✅ Сотрудник <b>{h(name)}</b> добавлен", ttl_sec=20)
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
            text=f"❌ Ошибка загрузки. Для фикса: {h(str(e))}",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data="add:cancel")]]
            ),
            parse_mode="HTML",
        )


def _parse_team_member_callback(data: str | None) -> tuple[int, int] | None:
    parts = (data or "").split(":")
    if len(parts) < 2 or not parts[1].isdigit():
        return None
    emp_id = int(parts[1])
    page = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 0
    return emp_id, max(0, page)


async def ui_render_team_member_card(
    message: Message,
    db_pool: asyncpg.Pool,
    *,
    emp_id: int,
    page: int = 0,
    preferred_message_id: int | None = None,
    force_new: bool = False,
) -> int:
    page_size = 8
    try:
        async with db_pool.acquire() as conn:
            tm = await conn.fetchrow("SELECT name, role, note FROM team WHERE id = $1", emp_id)
            if not tm:
                return await ui_render(
                    bot=message.bot,
                    db_pool=db_pool,
                    chat_id=int(message.chat.id),
                    text="❌ Сотрудник не найден.",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home")]]
                    ),
                    screen="team_member",
                    payload={"emp_id": emp_id, "page": page},
                    fallback_message=message,
                    parse_mode="HTML",
                    preferred_message_id=preferred_message_id,
                    force_new=force_new,
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
        note = str(tm["note"] or "").strip()

        lines: list[str] = []
        if role:
            lines.append(f"👤 <b>{h(name)}</b> — <i>{h(role)}</i>")
        else:
            lines.append(f"👤 <b>{h(name)}</b>")
        lines.append(f"📊 Активных задач: <b>{total_tasks}</b>")
        lines.append(f"🚨 Просрочено: <b>{overdue_count}</b>")
        lines.append("")
        if note:
            lines.append("<b>📝 Заметка</b>")
            lines.append(h(note).replace("\n", "<br>"))
        else:
            lines.append("<i>Заметки пока нет.</i>")

        kb: list[list[InlineKeyboardButton]] = []

        if not tasks:
            lines.append("")
            lines.append("✅ Сейчас нет активных задач.")
            kb.append([
                InlineKeyboardButton(
                    text=("📝 Редактировать заметку" if note else "📝 Добавить заметку"),
                    callback_data=f"teamnote:edit:{emp_id}:{page}",
                )
            ])
            kb.append([
                InlineKeyboardButton(text="⬅ Назад", callback_data="nav:team"),
                InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home"),
            ])
            return await ui_render(
                bot=message.bot,
                db_pool=db_pool,
                chat_id=int(message.chat.id),
                text="\n".join(lines).strip(),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
                screen="team_member",
                payload={"emp_id": emp_id, "page": page},
                fallback_message=message,
                parse_mode="HTML",
                preferred_message_id=preferred_message_id,
                force_new=force_new,
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
            InlineKeyboardButton(
                text=("📝 Редактировать заметку" if note else "📝 Добавить заметку"),
                callback_data=f"teamnote:edit:{emp_id}:{page}",
            )
        ])
        kb.append([
            InlineKeyboardButton(text="⬅ Назад", callback_data="nav:team"),
            InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home"),
        ])

        return await ui_render(
            bot=message.bot,
            db_pool=db_pool,
            chat_id=int(message.chat.id),
            text="\n".join(lines).strip(),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
            screen="team_member",
            payload={"emp_id": emp_id, "page": page},
            fallback_message=message,
            parse_mode="HTML",
            preferred_message_id=preferred_message_id,
            force_new=force_new,
        )

    except Exception as e:
        return await ui_render(
            bot=message.bot,
            db_pool=db_pool,
            chat_id=int(message.chat.id),
            text=f"❌ Ошибка загрузки. Для фикса: {h(str(e))}",
            reply_markup=back_home_kb(),
            screen="team_member",
            fallback_message=message,
            preferred_message_id=preferred_message_id,
            force_new=force_new,
            parse_mode="HTML",
        )


async def cb_team_member_details(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if not callback.from_user or callback.from_user.id != deps.admin_id:
        return await callback.answer("Недоступно", show_alert=True)
    async with db_pool.acquire() as conn:
        persona_mode = await get_persona_mode(conn, int(callback.message.chat.id))
    if is_solo_mode(persona_mode):
        await callback.answer("Режим Solo", show_alert=False)
        await state.clear()
        return await ui_render_team(callback.message, db_pool, force_new=False)

    await callback.answer()
    await state.clear()

    parsed = _parse_team_member_callback(callback.data)
    if not parsed:
        return

    emp_id, page = parsed
    await ui_render_team_member_card(callback.message, db_pool, emp_id=emp_id, page=page)

def _team_note_kb(emp_id: int, page: int, *, has_note: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if has_note:
        rows.append([InlineKeyboardButton(text="🗑 Очистить заметку", callback_data=f"teamnote:clear:{emp_id}:{page}")])
    rows.append([
        InlineKeyboardButton(text="⬅ Назад", callback_data=f"team:{emp_id}:{page}"),
        InlineKeyboardButton(text="✖️ Отмена", callback_data="add:cancel"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def cb_team_note_edit(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if not callback.from_user or callback.from_user.id != deps.admin_id:
        return await callback.answer("Недоступно", show_alert=True)
    async with db_pool.acquire() as conn:
        persona_mode = await get_persona_mode(conn, int(callback.message.chat.id))
    if is_solo_mode(persona_mode):
        await callback.answer("Режим Solo", show_alert=False)
        await state.clear()
        return await ui_render_team(callback.message, db_pool, force_new=False)
    await callback.answer()
    await state.clear()

    parts = (callback.data or "").split(":")
    if len(parts) < 3 or not parts[2].isdigit():
        return
    emp_id = int(parts[2])
    page = int(parts[3]) if len(parts) >= 4 and parts[3].isdigit() else 0

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT name, note FROM team WHERE id=$1", emp_id)
    if not row:
        return await safe_edit(callback.message, "❌ Сотрудник не найден.", reply_markup=back_home_kb(), parse_mode="HTML")

    note = str(row["note"] or "").strip()
    await state.update_data(
        team_note_emp_id=emp_id,
        team_note_page=page,
        team_note_has_note=bool(note),
        wizard_chat_id=int(callback.message.chat.id),
        wizard_msg_id=int(callback.message.message_id),
    )
    await state.set_state(EditTeamNoteWizard.entering)

    lines = [f"📝 <b>Заметка — {h(str(row['name'] or ''))}</b>", ""]
    if note:
        lines.extend(["Текущая заметка:", h(note), "", "Отправьте новый текст заметки сообщением."])
    else:
        lines.append("Отправьте короткую заметку сообщением. Она будет показана прямо в карточке сотрудника.")
    await wizard_render(
        bot=callback.bot,
        state=state,
        chat_id=int(callback.message.chat.id),
        fallback_msg=callback.message,
        text="\n".join(lines),
        reply_markup=_team_note_kb(emp_id, page, has_note=bool(note)),
        parse_mode="HTML",
    )


async def cb_team_note_clear(callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if not callback.from_user or callback.from_user.id != deps.admin_id:
        return await callback.answer("Недоступно", show_alert=True)
    async with db_pool.acquire() as conn:
        persona_mode = await get_persona_mode(conn, int(callback.message.chat.id))
    if is_solo_mode(persona_mode):
        await callback.answer("Режим Solo", show_alert=False)
        await state.clear()
        return await ui_render_team(callback.message, db_pool, force_new=False)
    await callback.answer()
    await state.clear()

    parts = (callback.data or "").split(":")
    if len(parts) < 3 or not parts[2].isdigit():
        return
    emp_id = int(parts[2])
    page = int(parts[3]) if len(parts) >= 4 and parts[3].isdigit() else 0

    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE team SET note='' WHERE id=$1", emp_id)
        ui_state = await ui_get_state(conn, int(callback.message.chat.id))
        payload = _ui_payload_get(ui_state)
        payload = ui_payload_with_toast(payload, "📝 Заметка очищена", ttl_sec=15)
        await ui_set_state(conn, int(callback.message.chat.id), ui_payload=payload)

    return await ui_render_team_member_card(callback.message, db_pool, emp_id=emp_id, page=page)


async def msg_team_note_save(message: Message, state: FSMContext, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    if deps.admin_id and (not message.from_user or message.from_user.id != deps.admin_id):
        return
    if await escape_hatch_menu_or_command(message, state, db_pool):
        return
    if not message.text:
        return
    await try_delete_user_message(message)

    note = (message.text or "").strip()
    if not note:
        data = await state.get_data()
        emp_id = int(data.get("team_note_emp_id") or 0)
        page = int(data.get("team_note_page") or 0)
        has_note = bool(data.get("team_note_has_note"))
        return await wizard_render(
            bot=message.bot,
            state=state,
            chat_id=int(message.chat.id),
            fallback_msg=None,
            text="Текст пустой. Отправьте заметку или очистите ее кнопкой ниже.",
            reply_markup=_team_note_kb(emp_id, page, has_note=has_note),
        )
    if len(note) > 500:
        data = await state.get_data()
        emp_id = int(data.get("team_note_emp_id") or 0)
        page = int(data.get("team_note_page") or 0)
        has_note = bool(data.get("team_note_has_note"))
        return await wizard_render(
            bot=message.bot,
            state=state,
            chat_id=int(message.chat.id),
            fallback_msg=None,
            text="Заметка слишком длинная. Оставьте до 500 символов.",
            reply_markup=_team_note_kb(emp_id, page, has_note=has_note),
        )

    data = await state.get_data()
    emp_id = int(data.get("team_note_emp_id") or 0)
    page = int(data.get("team_note_page") or 0)
    if emp_id <= 0:
        await state.clear()
        return await ui_render_team(message, db_pool, force_new=False)

    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE team SET note=$2 WHERE id=$1", emp_id, note)
        ui_state = await ui_get_state(conn, int(message.chat.id))
        payload = _ui_payload_get(ui_state)
        payload = ui_payload_with_toast(payload, "📝 Заметка сохранена", ttl_sec=15)
        await ui_set_state(conn, int(message.chat.id), ui_payload=payload)

    await state.clear()
    await ui_render_team_member_card(message, db_pool, emp_id=emp_id, page=page)

def register(dp: Dispatcher) -> None:
    dp.message.register(cmd_team_load, lambda m: m.text and canon(m.text) == "команда")

    dp.callback_query.register(cb_team_add, F.data == "team:add")
    dp.callback_query.register(cb_team_note_edit, F.data.startswith("teamnote:edit:"))
    dp.callback_query.register(cb_team_note_clear, F.data.startswith("teamnote:clear:"))
    dp.message.register(msg_team_add, StateFilter(AddTeamWizard.entering), F.text)
    dp.message.register(msg_team_note_save, StateFilter(EditTeamNoteWizard.entering), F.text)

    dp.callback_query.register(cb_team_member_details, F.data.startswith("team:"))
