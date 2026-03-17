"""Keyboards for the Today screen."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def today_screen_kb(has_tasks: bool) -> InlineKeyboardMarkup:
    kb = []

    # Row 1: actions
    row1 = []
    if has_tasks:
        row1.append(InlineKeyboardButton(text="📋 Список задач", callback_data="nav:today:pick:0"))
    row1.append(InlineKeyboardButton(text="🗂 Выполнено", callback_data="nav:today:done"))
    kb.append(row1)

    # Row 2: navigation
    kb.append(
        [
            InlineKeyboardButton(text="🔄 Обновить", callback_data="nav:today"),
            InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=kb)
