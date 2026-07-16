"""Common keyboards used across screens."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton


def main_menu_kb(persona_mode: str = "lead", *, llm_online: bool = True) -> ReplyKeyboardMarkup:
    """Persistent bottom keyboard focused on the four daily actions.

    Deeper surfaces such as reminders, team, work lists, and statistics remain
    available from the SPA screens, but no longer compete for attention in the
    always-visible Telegram keyboard.
    """
    del persona_mode  # Kept in the public signature for backward compatibility.
    add_button = "➕ Добавить" if llm_online else "⚠️ ИИ офлайн"
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📅 Сегодня"), KeyboardButton(text=add_button)],
            [KeyboardButton(text="📋 Все задачи"), KeyboardButton(text="📁 Проекты")],
            [KeyboardButton(text="↩️ Отмена")],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
        is_persistent=True,
        selective=True,
    )


def back_home_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home")]]
    )


def add_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📝 Задача", callback_data="add:task"),
                InlineKeyboardButton(text="🏡 Личная задача", callback_data="add:pers"),
            ],
            [
                InlineKeyboardButton(text="📅 Событие", callback_data="add:event"),
                InlineKeyboardButton(text="⏰ Напоминание", callback_data="add:rem"),
            ],
            [InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home")],
        ]
    )
