"""Task card UI (keyboard).

Kept separate from handlers so other screens (overdue/tails) can reuse the
same compact drill-down experience.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.persona import is_solo_mode


def task_deadline_kb(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📅 Сегодня 18:00", callback_data=f"task:{task_id}:dlset:today"),
                InlineKeyboardButton(text="📅 Завтра 18:00", callback_data=f"task:{task_id}:dlset:tomorrow"),
            ],
            [
                InlineKeyboardButton(text="+3 дня", callback_data=f"task:{task_id}:dlset:+3"),
                InlineKeyboardButton(text="+7 дней", callback_data=f"task:{task_id}:dlset:+7"),
            ],
            [
                InlineKeyboardButton(text="Без срока", callback_data=f"task:{task_id}:dlset:none"),
                InlineKeyboardButton(text="Ввести дату", callback_data=f"task:{task_id}:dlset:manual"),
            ],
            [
                InlineKeyboardButton(text="⬅ Назад", callback_data=f"task:{task_id}"),
                InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home"),
            ],
        ]
    )


def task_card_kb(
    task_id: int,
    project_id: int,
    parent_task_id: int | None,
    status: str,
    *,
    in_gtasks: bool = False,
    gtasks_dirty: bool = False,
    expanded: bool = False,
    subtasks: list[tuple[int, str]] | None = None,
    is_inbox: bool = False,
    triage: bool = False,
    return_cb: str | None = None,
    return_label: str | None = None,
    persona_mode: str = "lead",
) -> InlineKeyboardMarkup:
    """Task card keyboard (Drill-down + compact UI)."""

    status = (status or "todo").lower()
    fallback_back_cb = f"task:{int(parent_task_id)}" if parent_task_id else f"proj:{int(project_id)}"
    back_cb = (return_cb or "").strip() or fallback_back_cb
    back_label = (return_label or "").strip() or "⬅ Назад"

    def _triage_row() -> list[list[InlineKeyboardButton]]:
        if not triage:
            return []
        return [
            [
                InlineKeyboardButton(text="➡ Следующая", callback_data="inbox:triage:next"),
                InlineKeyboardButton(text="✖ Выйти", callback_data="inbox:triage:exit"),
            ]
        ]

    if not expanded:
        # Compact mode (daily use, minimal buttons)
        if status == "done":
            rows_done = [
                [
                    InlineKeyboardButton(text=back_label, callback_data=back_cb),
                    InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home"),
                ]
            ]
            rows_done.extend(_triage_row())
            return InlineKeyboardMarkup(inline_keyboard=rows_done)

        rows: list[list[InlineKeyboardButton]] = []
        rows.append([
            InlineKeyboardButton(text="✅ Готово", callback_data=f"task:{task_id}:done"),
            InlineKeyboardButton(text="🗓 Срок", callback_data=f"task:{task_id}:dl"),
        ])
        if is_inbox:
            rows.append([
                InlineKeyboardButton(text="📁 В проект…", callback_data=f"task:{task_id}:move"),
                InlineKeyboardButton(text="⚡ В работу", callback_data=f"task:{task_id}:in_progress"),
            ])
        else:
            if is_solo_mode(persona_mode):
                rows.append([InlineKeyboardButton(text="⚡ В работу", callback_data=f"task:{task_id}:in_progress")])
            else:
                rows.append([
                    InlineKeyboardButton(text="⚡ В работу", callback_data=f"task:{task_id}:in_progress"),
                    InlineKeyboardButton(text="👤 Исп.", callback_data=f"task:{task_id}:assignee"),
                ])

        rows.append([InlineKeyboardButton(text="⋯ Ещё", callback_data=f"task:{task_id}:more")])
        rows.append([
            InlineKeyboardButton(text=back_label, callback_data=back_cb),
            InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home"),
        ])

        rows.extend(_triage_row())
        return InlineKeyboardMarkup(inline_keyboard=rows)

    # Expanded (⋯ Ещё): secondary actions
    rows: list[list[InlineKeyboardButton]] = []
    rows.append([
        InlineKeyboardButton(text=back_label, callback_data=back_cb),
        InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home"),
    ])

    rows.append([
        InlineKeyboardButton(text="🧩 Связи…", callback_data=f"task:{task_id}:relations"),
        InlineKeyboardButton(text="↳ Подзадачи…", callback_data=f"task:{task_id}:subtasks"),
    ])
    if is_solo_mode(persona_mode):
        rows.append([InlineKeyboardButton(text="⏸ Отложить", callback_data=f"task:{task_id}:postpone")])
    else:
        rows.append([
            InlineKeyboardButton(text="👤 Исполнитель", callback_data=f"task:{task_id}:assignee"),
            InlineKeyboardButton(text="⏸ Отложить", callback_data=f"task:{task_id}:postpone"),
        ])
    rows.append([InlineKeyboardButton(text="⋯ Свернуть", callback_data=f"task:{task_id}:less")])

    rows.extend(_triage_row())

    return InlineKeyboardMarkup(inline_keyboard=rows)
