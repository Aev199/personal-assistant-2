import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot

from bot.handlers.common import escape_hatch_menu_or_command
from bot.handlers.system import msg_home_button


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
            patch("bot.handlers.common.ensure_main_menu", AsyncMock()),
        ):
            handled = await escape_hatch_menu_or_command(message, state, db_pool=object())

        self.assertTrue(handled)
        render_home.assert_awaited_once()
        # ensure_main_menu should be invoked so reply keyboard is restored
        bot.handlers.common.ensure_main_menu.assert_awaited_once()  # type: ignore[attr-defined]
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
            patch("bot.handlers.system.ensure_main_menu", AsyncMock()),
        ):
            await msg_home_button(message, state, db_pool=object(), deps=deps)

        render_home.assert_awaited_once()
        # anchor should be re-sent when menu button is handled
        bot.handlers.system.ensure_main_menu.assert_awaited_once()  # type: ignore[attr-defined]
        state.clear.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
