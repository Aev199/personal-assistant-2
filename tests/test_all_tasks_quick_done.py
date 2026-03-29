import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.ui.screens import ui_render_all_tasks


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
    async def fetchval(self, query, *_args):
        if "COUNT(*)" in query:
            return 1
        raise AssertionError(f"Unexpected fetchval query: {query}")

    async def fetch(self, query, *_args):
        if "FROM tasks t" in query:
            return [
                {
                    "id": 42,
                    "title": "Позвонить клиенту",
                    "project": "CRM",
                    "assignee": "—",
                    "deadline": None,
                }
            ]
        raise AssertionError(f"Unexpected fetch query: {query}")


class AllTasksQuickDoneTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_tasks_quick_done_mode_uses_done_callback(self) -> None:
        message = SimpleNamespace(chat=SimpleNamespace(id=201), bot=SimpleNamespace())
        pool = _Pool(_Conn())

        with (
            patch("bot.ui.screens._pop_screen_toast", AsyncMock(return_value=None)),
            patch("bot.ui.screens.ui_render", AsyncMock(return_value=1)) as render,
        ):
            await ui_render_all_tasks(message, pool, tz_name="Europe/Moscow", quick_done=True)

        kwargs = render.await_args.kwargs
        callbacks = [btn.callback_data for row in kwargs["reply_markup"].inline_keyboard for btn in row]
        self.assertIn("task:42:done_quick", callbacks)
        self.assertIn("nav:all:all:0", callbacks)
        self.assertIn("nav:all:today:qd1", callbacks)

    async def test_all_tasks_default_mode_keeps_open_task_callback(self) -> None:
        message = SimpleNamespace(chat=SimpleNamespace(id=202), bot=SimpleNamespace())
        pool = _Pool(_Conn())

        with (
            patch("bot.ui.screens._pop_screen_toast", AsyncMock(return_value=None)),
            patch("bot.ui.screens.ui_render", AsyncMock(return_value=1)) as render,
        ):
            await ui_render_all_tasks(message, pool, tz_name="Europe/Moscow", quick_done=False)

        kwargs = render.await_args.kwargs
        callbacks = [btn.callback_data for row in kwargs["reply_markup"].inline_keyboard for btn in row]
        self.assertIn("task:42", callbacks)
        self.assertIn("nav:all:all:0:qd1", callbacks)
        self.assertNotIn("task:42:done_quick", callbacks)


if __name__ == "__main__":
    unittest.main()
