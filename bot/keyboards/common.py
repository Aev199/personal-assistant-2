"""Common keyboards used across screens."""

from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
)


def main_menu_kb() -> ReplyKeyboardMarkup:
    """Persistent bottom keyboard: replaces typing commands."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📅 Сегодня"), KeyboardButton(text="🚨 Просрочки")],
            [KeyboardButton(text="📁 Проекты"), KeyboardButton(text="➕ Добавить")],
            [KeyboardButton(text="👥 Команда"), KeyboardButton(text="❓ Help")],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
        is_persistent=True,
        selective=True,
    )


def home_kb() -> InlineKeyboardMarkup:
    """Dashboard inline keyboard (Home screen). Navigation to sections is via bottom ReplyKeyboard."""
    kb = [
        [
            InlineKeyboardButton(text="⚡️ Быстрая задача", callback_data="quick:task"),
            InlineKeyboardButton(text="💡 Идея", callback_data="quick:idea"),
        ],
        [InlineKeyboardButton(text="🔄 Синхронизация", callback_data="sync:status")],
        [InlineKeyboardButton(text="🔄 Обновить сводку", callback_data="nav:home")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)


def back_home_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home")]]
    )


def add_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📝 Задача", callback_data="add:task"),
                InlineKeyboardButton(text="🏡 Личное (Tasks)", callback_data="add:pers"),
            ],
            [
                InlineKeyboardButton(text="📅 Событие (iCloud)", callback_data="add:event"),
                InlineKeyboardButton(text="⏰ Напоминание", callback_data="add:rem"),
            ],
            [InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home")],
        ]
    )
