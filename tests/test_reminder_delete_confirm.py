import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.handlers.reminders import cb_cancel_reminder_ask


class ReminderDeleteConfirmTests(unittest.IsolatedAsyncioTestCase):
    async def test_delete_ask_renders_confirmation_screen(self) -> None:
        callback = SimpleNamespace(
            data="rem:cancel_ask:12:2",
            answer=AsyncMock(),
            bot=SimpleNamespace(),
            message=SimpleNamespace(
                chat=SimpleNamespace(id=55),
                message_id=77,
            ),
        )

        with patch("bot.ui.render.ui_render", AsyncMock(return_value=77)) as render:
            await cb_cancel_reminder_ask(callback, db_pool=object(), deps=SimpleNamespace())

        callback.answer.assert_awaited_once()
        kwargs = render.await_args.kwargs
        self.assertEqual(kwargs["screen"], "reminder_delete_confirm")
        labels = [[btn.text for btn in row] for row in kwargs["reply_markup"].inline_keyboard]
        self.assertEqual(labels[0], ["🗑 Да, удалить"])
        self.assertEqual(labels[1], ["⬅ К списку"])


if __name__ == "__main__":
    unittest.main()
