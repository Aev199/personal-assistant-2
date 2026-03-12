import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.handlers.common import escape_hatch_menu_or_command
from bot.handlers.system import msg_home_button
from bot.ui.screens import ensure_main_menu


class _Acquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Pool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _Acquire(self._conn)


class _Conn:
    def __init__(self, menu_message_id):
        self.menu_message_id = menu_message_id
        self.execute = AsyncMock()

    async def fetchrow(self, _query, _chat_id):
        if self.menu_message_id is None:
            return None
        return {"menu_message_id": self.menu_message_id}


class MainMenuRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_escape_hatch_restores_home_for_main_menu_token(self) -> None:
        message = SimpleNamespace(
            text="🏠 Главное меню",
            chat=SimpleNamespace(id=101),
            bot=SimpleNamespace(),
        )
        state = AsyncMock()

        with (
            patch("bot.handlers.common.get_wizard_message_data", AsyncMock(return_value=(None, None))),
            patch("bot.handlers.common.try_delete_user_message", AsyncMock()),
            patch("bot.handlers.common.ui_render_home", AsyncMock(return_value=55)) as render_home,
            patch("bot.handlers.common.cleanup_stale_wizard_message", AsyncMock()),
            patch("bot.handlers.common.ensure_main_menu", AsyncMock(return_value=True)) as ensure_menu,
        ):
            handled = await escape_hatch_menu_or_command(message, state, db_pool=object())

        self.assertTrue(handled)
        render_home.assert_awaited_once()
        ensure_menu.assert_awaited_once()
        state.clear.assert_awaited_once()

    async def test_home_button_renders_home_screen(self) -> None:
        message = SimpleNamespace(
            text="🏠 Главное меню",
            chat=SimpleNamespace(id=202),
            from_user=SimpleNamespace(id=7),
            bot=SimpleNamespace(),
        )
        state = AsyncMock()
        deps = SimpleNamespace(admin_id=None, tz_name="Europe/Moscow")

        with (
            patch("bot.handlers.system._reply_wizard_context", AsyncMock(return_value=(None, None, None))),
            patch("bot.handlers.system.try_delete_user_message", AsyncMock()),
            patch("bot.handlers.system.ui_render_home", AsyncMock(return_value=77)) as render_home,
            patch("bot.handlers.system.cleanup_stale_wizard_message", AsyncMock()),
            patch("bot.handlers.system.ensure_main_menu", AsyncMock(return_value=True)) as ensure_menu,
        ):
            await msg_home_button(message, state, db_pool=object(), deps=deps)

        render_home.assert_awaited_once()
        ensure_menu.assert_awaited_once()
        state.clear.assert_awaited_once()

    async def test_ensure_main_menu_existing_anchor_is_noop_without_refresh(self) -> None:
        conn = _Conn(menu_message_id=55)
        pool = _Pool(conn)
        message = SimpleNamespace(
            chat=SimpleNamespace(id=101),
            bot=SimpleNamespace(edit_message_text=AsyncMock(), delete_message=AsyncMock()),
            answer=AsyncMock(),
        )

        sent = await ensure_main_menu(message, pool)

        self.assertFalse(sent)
        message.bot.edit_message_text.assert_not_awaited()
        message.answer.assert_not_awaited()

    async def test_ensure_main_menu_recreate_sends_new_anchor(self) -> None:
        conn = _Conn(menu_message_id=55)
        pool = _Pool(conn)
        message = SimpleNamespace(
            chat=SimpleNamespace(id=101),
            bot=SimpleNamespace(edit_message_text=AsyncMock(), delete_message=AsyncMock()),
            answer=AsyncMock(return_value=SimpleNamespace(message_id=77)),
        )

        sent = await ensure_main_menu(message, pool, recreate=True)

        self.assertTrue(sent)
        message.answer.assert_awaited_once()
        message.bot.delete_message.assert_awaited_once_with(chat_id=101, message_id=55)


if __name__ == "__main__":
    unittest.main()
