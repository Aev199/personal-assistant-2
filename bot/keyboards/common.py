"""Common keyboards used across screens."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton

from bot.persona import is_solo_mode


def main_menu_kb(persona_mode: str = "lead", *, llm_online: bool = True) -> ReplyKeyboardMarkup:
    """Persistent bottom keyboard: replaces typing commands."""
    last_button = "⚡ В работе" if is_solo_mode(persona_mode) else "👥 Команда"
    add_button = "➕ Добавить" if llm_online else "⚠️ ИИ офлайн"
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📅 Сегодня"), KeyboardButton(text="📋 Все задачи")],
            [KeyboardButton(text="📁 Проекты"), KeyboardButton(text="🔔 Напоминания")],
            [KeyboardButton(text=add_button), KeyboardButton(text=last_button)],
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
