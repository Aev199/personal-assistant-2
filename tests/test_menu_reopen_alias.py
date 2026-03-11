import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.handlers.common import escape_hatch_menu_or_command


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
    pass


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

    async def test_escape_hatch_retries_force_new_when_first_render_fails(self) -> None:
        message = SimpleNamespace(
            text="⌨️ Главное меню",
            chat=SimpleNamespace(id=99),
            bot=SimpleNamespace(delete_message=AsyncMock()),
        )
        state = AsyncMock()
        pool = _Pool(_Conn())

        with (
            patch("bot.handlers.common.get_wizard_message_data", AsyncMock(return_value=(99, 321))),
            patch("bot.handlers.common.try_delete_user_message", AsyncMock()),
            patch("bot.handlers.common.ui_set_state", AsyncMock()) as set_state,
            patch("bot.handlers.common.ui_render_home", AsyncMock(side_effect=[0, 456])) as render_home,
            patch("bot.handlers.common.cleanup_stale_wizard_message", AsyncMock()),
        ):
            handled = await escape_hatch_menu_or_command(message, state, db_pool=pool)

        self.assertTrue(handled)
        self.assertEqual(render_home.await_count, 2)
        first_kwargs = render_home.await_args_list[0].kwargs
        second_kwargs = render_home.await_args_list[1].kwargs
        self.assertFalse(first_kwargs["force_new"])
        self.assertTrue(second_kwargs["force_new"])
        set_state.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
