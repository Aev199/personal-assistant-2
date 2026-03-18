import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.ui.screens import ui_render_today


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
    def __init__(self):
        self.fetchval = AsyncMock(return_value=1)
        self.fetch = AsyncMock(
            side_effect=[
                [
                    {
                        "id": 42,
                        "title": "Позвонить клиенту",
                        "project": "CRM",
                        "assignee": "—",
                        "deadline": None,
                    }
                ],
                [],
            ]
        )


class TodayScreenContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_today_screen_is_action_ready_without_pick_screen(self) -> None:
        message = SimpleNamespace(chat=SimpleNamespace(id=101), bot=SimpleNamespace())
        conn = _Conn()
        pool = _Pool(conn)

        with (
            patch("bot.ui.screens._pop_screen_toast", AsyncMock(return_value=None)),
            patch("bot.ui.screens.ui_render", AsyncMock(return_value=1)) as render,
        ):
            await ui_render_today(message, pool, tz_name="Europe/Moscow", page=0)

        kwargs = render.await_args.kwargs
        self.assertEqual(kwargs["screen"], "today")
        callbacks = [btn.callback_data for row in kwargs["reply_markup"].inline_keyboard for btn in row]
        self.assertIn("task:42", callbacks)
        self.assertNotIn("nav:today:pick:0", callbacks)


if __name__ == "__main__":
    unittest.main()
