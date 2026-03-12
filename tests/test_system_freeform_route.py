import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.handlers.system import cmd_unknown


class SystemFreeformRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_cmd_unknown_routes_plain_text_to_freeform(self) -> None:
        message = SimpleNamespace(
            text="buy milk",
            chat=SimpleNamespace(id=404),
            from_user=SimpleNamespace(id=7),
        )
        state = AsyncMock()
        deps = SimpleNamespace(admin_id=None)

        with (
            patch("bot.handlers.system.try_delete_user_message", AsyncMock()),
            patch("bot.handlers.system.handle_freeform_text", AsyncMock(return_value=True)) as handle_freeform,
            patch("bot.handlers.system.ensure_main_menu", AsyncMock()),
        ):
            await cmd_unknown(message, state, deps, db_pool=object())

        state.clear.assert_awaited_once()
        handle_freeform.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
