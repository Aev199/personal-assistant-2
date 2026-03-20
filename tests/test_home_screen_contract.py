import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.ui.screens import ui_render_home


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


class _HomeConn:
    async def fetchval(self, query, *_args):
        if "SELECT current_project_id FROM user_settings" in query:
            return 7
        if "SELECT id FROM projects WHERE code='INBOX'" in query:
            return 99
        if "deadline IS NOT NULL AND deadline <" in query:
            return 2
        if "deadline IS NOT NULL AND deadline >= $1 AND deadline < $2" in query:
            return 3
        if "SELECT COUNT(*) FROM tasks t WHERE t.status='in_progress' AND t.kind != 'super'" in query:
            return 1
        if "SELECT COUNT(*) FROM tasks WHERE status != 'done' AND kind != 'super' AND project_id=$1" in query:
            return 4
        raise AssertionError(f"Unexpected fetchval query: {query}")

    async def fetchrow(self, query, *_args):
        if "SELECT code FROM projects WHERE id=$1" in query:
            return {"code": "ABC"}
        raise AssertionError(f"Unexpected fetchrow query: {query}")

    async def fetch(self, query, *_args):
        if "deadline < $1" in query:
            return [
                {
                    "id": 1,
                    "title": "Overdue task",
                    "project": "OPS",
                    "assignee": "Ира",
                    "deadline": datetime(2026, 3, 20, 7, 0, tzinfo=timezone.utc),
                }
            ]
        if "deadline >= $1 AND t.deadline < $2" in query:
            return [
                {
                    "id": 2,
                    "title": "Today task",
                    "project": "CRM",
                    "assignee": "Оля",
                    "deadline": datetime(2026, 3, 20, 10, 0, tzinfo=timezone.utc),
                }
            ]
        if "t.status='in_progress'" in query:
            return [
                {
                    "id": 3,
                    "title": "Work task",
                    "project": "OPS",
                    "assignee": "Маша",
                    "deadline": datetime(2026, 3, 20, 12, 0, tzinfo=timezone.utc),
                }
            ]
        raise AssertionError(f"Unexpected fetch query: {query}")


class HomeScreenContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_home_screen_is_more_compact_and_action_first(self) -> None:
        message = SimpleNamespace(chat=SimpleNamespace(id=11), bot=SimpleNamespace())
        pool = _Pool(_HomeConn())

        with (
            patch("bot.ui.screens._take_screen_payload", AsyncMock(return_value=({}, None))),
            patch("bot.ui.screens._pop_screen_toast", AsyncMock(return_value=None)),
            patch("bot.ui.screens.ui_set_state", AsyncMock()),
            patch("bot.ui.screens.ui_render", AsyncMock(return_value=77)) as render,
        ):
            await ui_render_home(message, pool, tz_name="Europe/Moscow")

        kwargs = render.await_args.kwargs
        self.assertEqual(kwargs["screen"], "home")
        text = kwargs["text"]
        self.assertIn("🔥 Срочно: <b>2</b> • ⏰ Сегодня: <b>3</b> • ⚡ В работе: <b>1</b> • 📥 Inbox: <b>4</b>", text)
        self.assertIn("<b>Ближайшее</b>", text)
        self.assertNotIn("<b>🔥 СРОЧНО</b>", text)
        self.assertNotIn("<b>⏰ СЕГОДНЯ</b>", text)
        self.assertNotIn("<b>⚡ В РАБОТЕ</b>", text)

        rows = [[btn.text for btn in row] for row in kwargs["reply_markup"].inline_keyboard]
        self.assertEqual(rows[0], ["⚡ Быстрая задача", "💡 Идея"])
        self.assertEqual(rows[1], ["📥 Inbox (4)", "🧹 Разобрать Inbox"])
        self.assertEqual(rows[2], ["⋯ Ещё"])


if __name__ == "__main__":
    unittest.main()
