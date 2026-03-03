"""Task card UI (keyboard).

Kept separate from handlers so other screens (overdue/tails) can reuse the
same compact drill-down experience.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def task_deadline_kb(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Сегодня 18:00", callback_data=f"task:{task_id}:dlset:today"),
                InlineKeyboardButton(text="Завтра 18:00", callback_data=f"task:{task_id}:dlset:tomorrow"),
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
    expanded: bool = False,
    subtasks: list[tuple[int, str]] | None = None,
) -> InlineKeyboardMarkup:
    """Task card keyboard (Drill-down + compact UI).

    Breadcrumbs:
    - if parent_task_id exists → back to parent task card
    - else → back to project card
    """

    status = (status or "todo").lower()
    back_cb = f"task:{int(parent_task_id)}" if parent_task_id else f"proj:{int(project_id)}"

    def _subtask_rows() -> list[list[InlineKeyboardButton]]:
        rows: list[list[InlineKeyboardButton]] = []
        if not subtasks:
            return rows

        def _short(s: str, n: int = 30) -> str:
            s = (s or "").strip()
            return s if len(s) <= n else (s[: n - 1] + "…")

        for sid, title in subtasks:
            rows.append([InlineKeyboardButton(text=f"↳ {_short(title, 30)}", callback_data=f"task:{int(sid)}")])
        return rows

    if not expanded:
        # Compact mode
        if status == "done":
            return InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="⬅ Назад", callback_data=back_cb),
                        InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home"),
                    ]
                ]
            )

        rows: list[list[InlineKeyboardButton]] = []

        rows.append(
            [
                InlineKeyboardButton(text="✅ Готово", callback_data=f"task:{task_id}:done"),
                InlineKeyboardButton(text="⏸ Отложить", callback_data=f"task:{task_id}:postpone"),
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(text="👤 Исполнитель", callback_data=f"task:{task_id}:assignee"),
                InlineKeyboardButton(text="🗓 Срок", callback_data=f"task:{task_id}:dl"),
            ]
        )

        # Relations
        rel_row: list[InlineKeyboardButton] = [InlineKeyboardButton(text="➕ Подзадача", callback_data=f"add:sub:{task_id}")]
        if parent_task_id:
            rel_row.append(InlineKeyboardButton(text="⛓ Отвязать", callback_data=f"task:{task_id}:detach"))
        else:
            rel_row.append(InlineKeyboardButton(text="🔗 В подзадачи", callback_data=f"task:{task_id}:parent"))
        rows.append(rel_row)

        # Export to Google Tasks (fallback)
        if in_gtasks:
            rows.append([InlineKeyboardButton(text="✅ Google Tasks", callback_data=f"task:{task_id}:gtasks")])
        else:
            rows.append([InlineKeyboardButton(text="📤 В Google Tasks", callback_data=f"task:{task_id}:gtasks")])

        # Inbox triage: move to another project
        rows.append([InlineKeyboardButton(text="📁 В проект", callback_data=f"task:{task_id}:move")])

        rows.extend(_subtask_rows())

        rows.append(
            [
                InlineKeyboardButton(text="⬅ Главное", callback_data=f"task:{task_id}:more"),
                InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home"),
            ]
        )
        return InlineKeyboardMarkup(inline_keyboard=rows)

    # Expanded mode
    rows: list[list[InlineKeyboardButton]] = []

    if status != "done":
        rows.append(
            [
                InlineKeyboardButton(text="✅ Готово", callback_data=f"task:{task_id}:done"),
                InlineKeyboardButton(text="⏳ В работе", callback_data=f"task:{task_id}:in_progress"),
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(text="⏸ Отложить", callback_data=f"task:{task_id}:postpone"),
                InlineKeyboardButton(text="👤 Исполнитель", callback_data=f"task:{task_id}:assignee"),
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(text="🗓 Срок", callback_data=f"task:{task_id}:dl"),
                InlineKeyboardButton(text="🔗 Родитель", callback_data=f"task:{task_id}:parent"),
            ]
        )
        rows.append([InlineKeyboardButton(text="➕ Подзадача", callback_data=f"add:sub:{task_id}")])
        if parent_task_id:
            rows.append([InlineKeyboardButton(text="⛓ Отвязать", callback_data=f"task:{task_id}:detach")])

    if in_gtasks:
        rows.append([InlineKeyboardButton(text="✅ Google Tasks", callback_data=f"task:{task_id}:gtasks")])
    else:
        rows.append([InlineKeyboardButton(text="📤 В Google Tasks", callback_data=f"task:{task_id}:gtasks")])

    rows.append([InlineKeyboardButton(text="📁 В проект", callback_data=f"task:{task_id}:move")])
    rows.extend(_subtask_rows())
    rows.append(
        [
            InlineKeyboardButton(text="⬅ Назад", callback_data=f"task:{task_id}:less"),
            InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)
