import unittest
from datetime import datetime, timezone
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
    def __init__(self, *, events=None, total_events: int = 0):
        self._events = list(events or [])
        self._total_events = total_events

    async def fetchval(self, query, *_args):
        if "FROM tasks t" in query and "COUNT(*)" in query:
            return 1
        if "FROM icloud_events" in query and "COUNT(*)" in query:
            return self._total_events
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
        if "FROM reminders" in query:
            return []
        if "FROM icloud_events" in query:
            return self._events
        raise AssertionError(f"Unexpected fetch query: {query}")


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

    async def test_today_screen_shows_calendar_events_summary(self) -> None:
        message = SimpleNamespace(chat=SimpleNamespace(id=102), bot=SimpleNamespace())
        conn = _Conn(
            total_events=4,
            events=[
                {
                    "calendar_url": "work://calendar",
                    "summary": "Дейли",
                    "dtstart_utc": datetime(2026, 3, 18, 7, 0, tzinfo=timezone.utc),
                    "dtend_utc": datetime(2026, 3, 18, 7, 30, tzinfo=timezone.utc),
                },
                {
                    "calendar_url": "personal://calendar",
                    "summary": "Спортзал",
                    "dtstart_utc": datetime(2026, 3, 18, 16, 0, tzinfo=timezone.utc),
                    "dtend_utc": datetime(2026, 3, 18, 17, 0, tzinfo=timezone.utc),
                },
                {
                    "calendar_url": "other://calendar",
                    "summary": "Созвон",
                    "dtstart_utc": datetime(2026, 3, 18, 18, 0, tzinfo=timezone.utc),
                    "dtend_utc": datetime(2026, 3, 18, 19, 0, tzinfo=timezone.utc),
                },
            ],
        )
        pool = _Pool(conn)

        with (
            patch("bot.ui.screens._pop_screen_toast", AsyncMock(return_value=None)),
            patch("bot.ui.screens.os.getenv", side_effect=lambda key: {"ICLOUD_CALENDAR_URL_WORK": "work://calendar", "ICLOUD_CALENDAR_URL_PERSONAL": "personal://calendar"}.get(key, "")),
            patch("bot.ui.screens.ui_render", AsyncMock(return_value=1)) as render,
        ):
            await ui_render_today(message, pool, tz_name="Europe/Moscow", page=0)

        text = render.await_args.kwargs["text"]
        self.assertIn("Событий: 4", text)
        self.assertIn("<b>📅 События</b>", text)
        self.assertIn("💼 <b>10:00–10:30</b> • Дейли", text)
        self.assertIn("🏡 <b>19:00–20:00</b> • Спортзал", text)
        self.assertIn("📅 <b>21:00–22:00</b> • Созвон", text)
        self.assertIn("… ещё 1", text)


if __name__ == "__main__":
    unittest.main()
