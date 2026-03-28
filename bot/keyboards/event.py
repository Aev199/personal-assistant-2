"""Keyboards for iCloud event wizard."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def event_kind_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="💼 Работа", callback_data="ev:kind:work"),
                InlineKeyboardButton(text="🏡 Личное", callback_data="ev:kind:personal"),
            ],
            [InlineKeyboardButton(text="✖️ Отмена", callback_data="ev:cancel"), InlineKeyboardButton(text="⬅️ Домой", callback_data="ev:cancel")],
        ]
    )


def event_title_kb(kind: str, project_code: str | None, include_project: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if kind == "work" and project_code:
        if include_project:
            rows.append([InlineKeyboardButton(text=f"✅ Проект: {project_code}", callback_data="ev:proj:toggle")])
        else:
            rows.append([InlineKeyboardButton(text=f"➕ Вставить проект: {project_code}", callback_data="ev:proj:toggle")])
    rows.append([InlineKeyboardButton(text="✖️ Отмена", callback_data="ev:cancel"), InlineKeyboardButton(text="⬅️ Домой", callback_data="ev:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def event_date_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Сегодня", callback_data="ev:date:today"),
                InlineKeyboardButton(text="Завтра", callback_data="ev:date:tomorrow"),
            ],
            [InlineKeyboardButton(text="Ввести дату", callback_data="ev:date:manual")],
            [InlineKeyboardButton(text="✖️ Отмена", callback_data="ev:cancel"), InlineKeyboardButton(text="⬅️ Домой", callback_data="ev:cancel")],
        ]
    )


def event_time_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="09:00", callback_data="ev:time:09:00"),
                InlineKeyboardButton(text="10:00", callback_data="ev:time:10:00"),
            ],
            [
                InlineKeyboardButton(text="14:00", callback_data="ev:time:14:00"),
                InlineKeyboardButton(text="16:00", callback_data="ev:time:16:00"),
            ],
            [InlineKeyboardButton(text="Ввести время", callback_data="ev:time:manual")],
            [InlineKeyboardButton(text="✖️ Отмена", callback_data="ev:cancel"), InlineKeyboardButton(text="⬅️ Домой", callback_data="ev:cancel")],
        ]
    )


def event_duration_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="30 мин", callback_data="ev:dur:30"),
                InlineKeyboardButton(text="60 мин", callback_data="ev:dur:60"),
            ],
            [
                InlineKeyboardButton(text="90 мин", callback_data="ev:dur:90"),
                InlineKeyboardButton(text="120 мин", callback_data="ev:dur:120"),
            ],
            [InlineKeyboardButton(text="Ввести (мин)", callback_data="ev:dur:manual")],
            [InlineKeyboardButton(text="✖️ Отмена", callback_data="ev:cancel"), InlineKeyboardButton(text="⬅️ Домой", callback_data="ev:cancel")],
        ]
    )


def event_confirm_kb(kind: str = "work") -> InlineKeyboardMarkup:
    toggle_txt = "🏡 Сделать личным" if kind == "work" else "💼 Сделать рабочим"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Создать", callback_data="ev:create")],
            [InlineKeyboardButton(text=toggle_txt, callback_data="ev:toggle_kind")],
            [InlineKeyboardButton(text="🕒 Изменить дату/время", callback_data="ev:edit_datetime")],
            [InlineKeyboardButton(text="⏱ Изменить длительность", callback_data="ev:edit_duration")],
            [InlineKeyboardButton(text="✖️ Отмена", callback_data="ev:cancel"), InlineKeyboardButton(text="⬅️ Домой", callback_data="ev:cancel")],
        ]
    )
