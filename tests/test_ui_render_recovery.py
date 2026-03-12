import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramBadRequest

from bot.ui.render import ui_render


class DummyConn:
    pass


class DummyPool:
    def __init__(self):
        self.conn = DummyConn()

    async def acquire(self):
        class Ctx:
            async def __aenter__(self_inner):
                return self.conn

            async def __aexit__(self_inner, exc_type, exc, tb):
                return False

        return Ctx()


class UiRenderRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_not_modified_error_triggers_new_message(self) -> None:
        # simulate a stored UI message id that does not actually exist; editing
        # it raises a "message is not modified" error, and we expect a fresh
        # send to happen instead of silently giving up.
        bot = SimpleNamespace()
        bot.edit_message_text = AsyncMock(side_effect=TelegramBadRequest("message is not modified"))
        bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=999))

        # state returned by ui_get_state (we patch this so no DB access occurs)
        fake_state = {"ui_message_id": 42, "ui_screen": "home", "ui_payload": {}}

        with patch("bot.ui.render.ui_get_state", AsyncMock(return_value=fake_state)), \
             patch("bot.ui.render.ui_set_state", AsyncMock()):
            pool = DummyPool()
            msg_id = await ui_render(
                bot=bot,
                db_pool=pool,
                chat_id=123,
                text="hello",
                reply_markup=None,
            )

        # ensure we attempted to send a new message after the bogus edit
        bot.send_message.assert_awaited_once()
        self.assertEqual(msg_id, 999)


if __name__ == "__main__":
    unittest.main()
