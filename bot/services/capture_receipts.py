"""Durable receipts for multi-item capture; all navigation uses the SPA anchor.

Receipts retain the original input and unresolved items without turning personal
items into work tasks. Pending-action statuses remain the execution authority.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime, timezone

from aiogram.types import InlineKeyboardButton as Button, InlineKeyboardMarkup

from bot.db.runtime_state import get_conversation_state, set_conversation_state
from bot.ui.render import ui_render
from bot.utils import h

PAGE_SIZE = 5
SOURCE_SIZE = 200


def _preview(value, budget=150):
    """Bound escaped HTML, including emoji, without splitting an entity or tag."""
    value = str(value)
    if len(h(value).encode("utf-16-le")) // 2 <= budget:
        return h(value)
    value = value[:budget]
    while value and len(h(value).encode("utf-16-le")) // 2 > budget - 1:
        value = value[:-1]
    return h(value) + "…"


def receipt_id(message) -> str:
    return hashlib.sha256(f"{message.chat.id}:{message.message_id}".encode()).hexdigest()[:16]


async def save_receipt(pool, chat_id, capture_id, receipt):
    async with pool.acquire() as conn:
        await set_conversation_state(conn, chat_id, f"capture:{capture_id}",
                                     step="saved", payload=receipt, ttl_sec=None)


async def load_receipt(pool, chat_id, capture_id):
    async with pool.acquire() as conn:
        row = await get_conversation_state(conn, chat_id, f"capture:{capture_id}")
    return row["payload"] if row else None


def item_title(item):
    intent = item.get("intent") or {}
    return str(intent.get("title") or intent.get("reminder_text") or intent.get("idea_text") or "Запись")


def item_status(item, pending):
    action_id = item.get("pending_action_id")
    if not action_id:
        return "Нужны детали" if (item.get("intent") or {}).get("needs_followup") else "Не обработано"
    action = pending.get(int(action_id))
    if not action:
        return "Проверить результат"
    status = action["status"]
    if status == "pending":
        expires = action.get("expires_at")
        if expires and expires <= datetime.now(timezone.utc):
            return "Черновик истёк"
    return {"executed": "Создано", "pending": "Подтвердить", "failed": "Ошибка",
            "cancelled": "Отменено", "confirmed": "Обрабатывается", "expired": "Черновик истёк"}.get(status, "Проверить результат")


async def render_receipt(message, pool, capture_id, *, page=0, toast=None, source_page=None):
    chat_id = int(message.chat.id)
    receipt = await load_receipt(pool, chat_id, capture_id)
    if not receipt:
        return await ui_render(bot=message.bot, db_pool=pool, chat_id=chat_id,
                               text="Запись не найдена.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                   [Button(text="Домой", callback_data="nav:home")]]),
                               screen="capture", payload={}, fallback_message=message)
    items = receipt.get("items") or []
    ids = [int(item["pending_action_id"]) for item in items if item.get("pending_action_id")]
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, status, expires_at, payload_json FROM pending_actions WHERE chat_id=$1 AND id=ANY($2::bigint[])",
            chat_id, ids,
        ) if ids else []
    pending = {int(row["id"]): dict(row) for row in rows}
    counts = Counter(item_status(item, pending) for item in items)
    page = min(max(0, int(page)), max(0, (len(items)-1)//PAGE_SIZE))
    lines = ([_preview(toast, 160), ""] if toast else []) + ["<b>Запись сохранена</b>"]
    for status, count in counts.items():
        lines.append(f"{status}: {count}")
    if not items:
        lines.append("Пока не удалось выделить дела. Исходный текст сохранён.")
    if counts["Нужны детали"] or counts["Не обработано"] or counts["Ошибка"] or counts["Проверить результат"]:
        lines.append("Необработанные пункты сохранены здесь; задачи и напоминания для них ещё не подтверждены.")
    buttons = []
    labels = {"task": "Работа", "personal_task": "Личное", "idea": "Идея", "reminder": "Напоминание", "event": "Событие"}
    for index, item in enumerate(items[page*PAGE_SIZE:(page+1)*PAGE_SIZE], page*PAGE_SIZE):
        intent = dict(item.get("intent") or {})
        current = pending.get(int(item.get("pending_action_id") or 0), {})
        actual = current.get("payload_json") or {}
        if isinstance(actual, str):
            actual = json.loads(actual)
        if actual:
            intent.update(actual)
            if intent.get("action") == "event" and actual.get("calendar_kind") == "personal":
                intent.pop("project_code", None)
                intent.pop("project_name", None)
        status = item_status(item, pending)
        title = _preview(item_title(item), 150)
        category = labels.get(intent.get("followup_action") or intent.get("action"), "Запись")
        if intent.get("action") == "event":
            category += " · рабочее" if intent.get("calendar_kind") == "work" else " · личное"
        details = [category]
        if intent.get("project_code") or intent.get("project_name"):
            details.append(str(intent.get("project_code") or intent.get("project_name")))
        when = intent.get("deadline_local") or intent.get("remind_at_local") or intent.get("start_at_local")
        if when:
            details.append(str(when))
        lines.extend(["", f"<b>{index+1}. {title}</b>", _preview(" · ".join(details), 120), h(status)])
        if status == "Подтвердить":
            buttons.append([Button(text=f"Проверить пункт {index+1}",
                                   callback_data=f"capture:draft:{capture_id}:{int(item['pending_action_id'])}")])
    nav = []
    if page:
        nav.append(Button(text="Назад", callback_data=f"capture:open:{capture_id}:{page-1}"))
    if (page+1)*PAGE_SIZE < len(items):
        nav.append(Button(text="Далее", callback_data=f"capture:open:{capture_id}:{page+1}"))
    if nav:
        buttons.append(nav)
    raw = str(receipt.get("raw_text") or "")
    # Bound plain HTML before escaping; pagination retains access to every byte.
    raw_page = max(0, min(int(source_page or 0), max(0, (len(raw)-1)//SOURCE_SIZE)))
    chunk = raw[raw_page*SOURCE_SIZE:(raw_page+1)*SOURCE_SIZE]
    source_label = f"Исходное сообщение · {raw_page+1}/{max(1, (len(raw)+SOURCE_SIZE-1)//SOURCE_SIZE)}"
    base = "\n".join(lines)
    rich_html = base.replace("\n", "<br>") + f"<details><summary>{source_label}</summary><p>{h(chunk).replace(chr(10), '<br>')}</p></details>"
    text = base + f"\n\n<b>{source_label}</b>\n<blockquote expandable>{h(chunk)}</blockquote>"
    source_nav = []
    if raw_page:
        source_nav.append(Button(text="← Исходный текст", callback_data=f"capture:source:{capture_id}:{raw_page-1}"))
    if (raw_page+1)*SOURCE_SIZE < len(raw):
        source_nav.append(Button(text="Исходный текст →", callback_data=f"capture:source:{capture_id}:{raw_page+1}"))
    if source_nav:
        buttons.append(source_nav)
    buttons.append([Button(text="Записи", callback_data="capture:list:0"), Button(text="Домой", callback_data="nav:home")])
    return await ui_render(bot=message.bot, db_pool=pool, chat_id=chat_id, text=text,
                           rich_html=rich_html, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
                           screen="capture", payload={"capture_id": capture_id, "capture_page": page},
                           fallback_message=message, parse_mode="HTML")


async def render_receipt_list(message, pool, *, page=0):
    page = max(0, int(page))
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT flow, payload_json->>'raw_text' AS raw_text FROM conversation_state "
            "WHERE chat_id=$1 AND flow LIKE 'capture:%' ORDER BY updated_at DESC, flow "
            "LIMIT 9 OFFSET $2", int(message.chat.id), page*8,
        )
    buttons = [[Button(text=str(row['raw_text'] or 'Запись')[:60],
                       callback_data=f"capture:open:{row['flow'].split(':')[1]}:0")] for row in rows[:8]]
    nav = []
    if page:
        nav.append(Button(text="Назад", callback_data=f"capture:list:{page-1}"))
    if len(rows) > 8:
        nav.append(Button(text="Далее", callback_data=f"capture:list:{page+1}"))
    if nav:
        buttons.append(nav)
    buttons.append([Button(text="Домой", callback_data="nav:home")])
    return await ui_render(bot=message.bot, db_pool=pool, chat_id=int(message.chat.id),
                           text="<b>Записи</b>\nИсходные сообщения и результаты разбора." if rows else "Сохранённых списков пока нет.",
                           reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), screen="captures",
                           payload={"page": page}, fallback_message=message)
