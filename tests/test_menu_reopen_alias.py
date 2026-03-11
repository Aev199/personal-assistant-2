import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.handlers.common import escape_hatch_menu_or_command


class MenuReopenAliasTests(unittest.IsolatedAsyncioTestCase):
    async def test_escape_hatch_handles_main_menu_alias(self) -> None:
        message = SimpleNamespace(
            text="⌨️ Главное меню",
            chat=SimpleNamespace(id=77),
            bot=SimpleNamespace(delete_message=AsyncMock()),
        )
        state = AsyncMock()

        with (
            patch("bot.handlers.common.get_wizard_message_data", AsyncMock(return_value=(77, 123))),
            patch("bot.handlers.common.try_delete_user_message", AsyncMock()),
            patch("bot.handlers.common.ui_render_home", AsyncMock(return_value=123)) as render_home,
            patch("bot.handlers.common.cleanup_stale_wizard_message", AsyncMock()) as cleanup,
        ):
            handled = await escape_hatch_menu_or_command(message, state, db_pool=SimpleNamespace())

        self.assertTrue(handled)
        state.clear.assert_awaited_once()
        render_home.assert_awaited_once()
        cleanup.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
