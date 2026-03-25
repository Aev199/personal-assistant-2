import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.fsm import AddTaskWizard
from bot.handlers.wizards import (
    _task_confirm_kb,
    cb_add_task_start,
    cb_quick_task,
    msg_add_task_title,
)


class AddTaskCaptureFlowTests(unittest.IsolatedAsyncioTestCase):
    def test_confirm_keyboard_keeps_edit_actions_on_single_confirm_screen(self) -> None:
        kb = _task_confirm_kb("lead")
        rows = [[btn.text for btn in row] for row in kb.inline_keyboard]

        self.assertEqual(rows[0], ["✅ Создать"])
        self.assertEqual(rows[1], ["📁 Проект", "👤 Исполнитель"])
        self.assertEqual(rows[2], ["🗓 Срок"])
        self.assertEqual(rows[3], ["✖️ Отмена"])

    def test_confirm_keyboard_hides_assignee_in_solo(self) -> None:
        kb = _task_confirm_kb("solo")
        rows = [[btn.text for btn in row] for row in kb.inline_keyboard]

        self.assertEqual(rows[0], ["✅ Создать"])
        self.assertEqual(rows[1], ["📁 Проект"])
        self.assertEqual(rows[2], ["🗓 Срок"])
        self.assertEqual(rows[3], ["✖️ Отмена"])

    async def test_quick_task_routes_into_capture_first_flow_with_inbox_default(self) -> None:
        callback = SimpleNamespace(
            data="quick:task",
            answer=AsyncMock(),
            message=SimpleNamespace(chat=SimpleNamespace(id=77)),
        )
        state = AsyncMock()
        deps = SimpleNamespace(admin_id=None)

        with patch("bot.handlers.wizards._start_task_capture", AsyncMock()) as start_capture:
            await cb_quick_task(callback, state, db_pool=object(), deps=deps)

        callback.answer.assert_awaited_once()
        kwargs = start_capture.await_args.kwargs
        self.assertTrue(kwargs["default_inbox"])
        self.assertIsNone(kwargs["forced_project_id"])
        self.assertIsNone(kwargs["parent_task_id"])

    async def test_add_task_start_prefills_forced_project_when_opened_from_project(self) -> None:
        callback = SimpleNamespace(
            data="add:task:42",
            answer=AsyncMock(),
            message=SimpleNamespace(chat=SimpleNamespace(id=77)),
        )
        state = AsyncMock()
        deps = SimpleNamespace(admin_id=None)

        with patch("bot.handlers.wizards._start_task_capture", AsyncMock()) as start_capture:
            await cb_add_task_start(callback, state, db_pool=object(), deps=deps)

        callback.answer.assert_awaited_once()
        kwargs = start_capture.await_args.kwargs
        self.assertEqual(kwargs["forced_project_id"], 42)
        self.assertFalse(kwargs["default_inbox"])

    async def test_title_entry_moves_directly_to_confirm(self) -> None:
        message = SimpleNamespace(
            text="Подготовить демо",
            chat=SimpleNamespace(id=55),
            from_user=SimpleNamespace(id=9),
            bot=SimpleNamespace(),
        )
        state = AsyncMock()
        deps = SimpleNamespace(admin_id=None, tz_name="Europe/Moscow")
        db_pool = object()

        with (
            patch("bot.handlers.wizards.escape_hatch_menu_or_command", AsyncMock(return_value=False)),
            patch("bot.handlers.wizards.try_delete_user_message", AsyncMock()),
            patch("bot.handlers.wizards.quick_parse_datetime_ru", return_value=None),
            patch("bot.handlers.wizards._task_render_confirm", AsyncMock()) as render_confirm,
        ):
            await msg_add_task_title(message, state, db_pool=db_pool, deps=deps)

        state.update_data.assert_awaited_once_with(title="Подготовить демо")
        state.set_state.assert_awaited_once_with(AddTaskWizard.confirming)
        render_confirm.assert_awaited_once_with(message, state, db_pool, deps)


if __name__ == "__main__":
    unittest.main()
