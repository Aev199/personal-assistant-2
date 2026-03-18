import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.fsm import AddReminderWizard
from bot.handlers.wizards import (
    cb_add_reminder_back,
    cb_add_reminder_time,
    msg_add_reminder_time,
    reminder_repeat_kb,
)


class ReminderWizardResilienceTests(unittest.IsolatedAsyncioTestCase):
    async def test_manual_time_prompt_keeps_back_and_cancel(self) -> None:
        callback = SimpleNamespace(
            data="add:rtime:manual",
            answer=AsyncMock(),
            bot=SimpleNamespace(),
            message=SimpleNamespace(chat=SimpleNamespace(id=55)),
        )
        state = AsyncMock()
        deps = SimpleNamespace(admin_id=None, tz_name="Europe/Moscow")

        with patch("bot.handlers.wizards.wizard_render", AsyncMock()) as render:
            await cb_add_reminder_time(callback, state, deps)

        state.set_state.assert_awaited_once_with(AddReminderWizard.entering_time)
        labels = [[btn.text for btn in row] for row in render.await_args.kwargs["reply_markup"].inline_keyboard]
        self.assertEqual(labels[0], ["⬅ Назад", "✖️ Отмена"])

    async def test_invalid_manual_time_keeps_back_and_cancel(self) -> None:
        message = SimpleNamespace(
            text="не дата",
            chat=SimpleNamespace(id=77),
            from_user=SimpleNamespace(id=3),
            bot=SimpleNamespace(),
        )
        state = AsyncMock()
        deps = SimpleNamespace(admin_id=None, tz_name="Europe/Moscow")

        with (
            patch("bot.handlers.wizards.escape_hatch_menu_or_command", AsyncMock(return_value=False)),
            patch("bot.handlers.wizards.try_delete_user_message", AsyncMock()),
            patch("bot.handlers.wizards.asyncio.to_thread", AsyncMock(return_value=None)),
            patch("bot.handlers.wizards.wizard_render", AsyncMock()) as render,
        ):
            await msg_add_reminder_time(message, state, db_pool=object(), deps=deps)

        labels = [[btn.text for btn in row] for row in render.await_args.kwargs["reply_markup"].inline_keyboard]
        self.assertEqual(labels[0], ["⬅ Назад", "✖️ Отмена"])

    def test_repeat_keyboard_has_back_and_cancel(self) -> None:
        kb = reminder_repeat_kb()
        rows = [[btn.text for btn in row] for row in kb.inline_keyboard]
        self.assertEqual(rows[-1], ["⬅ Назад", "✖️ Отмена"])

    async def test_back_from_repeat_returns_to_text_prompt(self) -> None:
        callback = SimpleNamespace(
            data="add:rback:text",
            answer=AsyncMock(),
            bot=SimpleNamespace(),
            message=SimpleNamespace(chat=SimpleNamespace(id=88)),
        )
        state = AsyncMock()
        state.get_data = AsyncMock(return_value={"remind_at": "2026-03-19T09:00:00+00:00"})
        deps = SimpleNamespace(admin_id=None, tz_name="Europe/Moscow")

        with patch("bot.handlers.wizards._render_reminder_text_prompt", AsyncMock()) as render_text:
            await cb_add_reminder_back(callback, state, deps)

        state.set_state.assert_awaited_once_with(AddReminderWizard.entering_text)
        render_text.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
