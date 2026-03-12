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

    def acquire(self):
        class Ctx:
            async def __aenter__(self_inner):
                return self.conn

            async def __aexit__(self_inner, exc_type, exc, tb):
                return False

        return Ctx()


class UiRenderRecoveryTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _bad_request(message: str) -> TelegramBadRequest:
        return TelegramBadRequest(SimpleNamespace(), message)

    async def test_not_modified_error_is_treated_as_success(self) -> None:
        bot = SimpleNamespace()
        bot.edit_message_text = AsyncMock(side_effect=self._bad_request("message is not modified"))
        bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=999))

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

        bot.send_message.assert_not_awaited()
        self.assertEqual(msg_id, 42)

    async def test_missing_ui_message_triggers_new_message(self) -> None:
        bot = SimpleNamespace()
        bot.edit_message_text = AsyncMock(side_effect=self._bad_request("message to edit not found"))
        bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=999))

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

        bot.send_message.assert_awaited_once()
        self.assertEqual(msg_id, 999)


if __name__ == "__main__":
    unittest.main()
