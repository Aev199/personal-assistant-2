import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from bot.ui import simple_daily


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


class _Conn:
    async def fetchval(self, query, *_args):
        if "COUNT(*)" in query:
            return 2
        raise AssertionError(query)

    async def fetch(self, query, *_args):
        if "FROM tasks t" in query:
            assert "ORDER BY p.code" not in query
            assert "CASE" in query
            return [
                {
                    "id": 1,
                    "title": "Проверить расчёт",
                    "project": "INBOX",
                    "assignee": "—",
                    "deadline": None,
                    "status": "todo",
                    "created_at": None,
                },
                {
                    "id": 2,
                    "title": "Выдать нагрузки",
                    "project": "BAG",
                    "assignee": "Иванов",
                    "deadline": None,
                    "status": "todo",
                    "created_at": None,
                },
            ]
        raise AssertionError(query)


class SimpleDailyUiTests(unittest.IsolatedAsyncioTestCase):
    async def test_home_is_one_unfiltered_urgency_sorted_list(self) -> None:
        message = SimpleNamespace(chat=SimpleNamespace(id=1), bot=SimpleNamespace())
        with (
            patch.object(simple_daily.legacy, "_pop_screen_toast", AsyncMock(return_value=None)),
            patch.object(simple_daily, "ui_render", AsyncMock(return_value=7)) as render,
        ):
            await simple_daily.ui_render_all_tasks(message, _Pool(_Conn()), tz_name="Europe/Moscow")

        kwargs = render.await_args.kwargs
        callbacks = [b.callback_data for row in kwargs["reply_markup"].inline_keyboard for b in row]
        labels = [b.text for row in kwargs["reply_markup"].inline_keyboard for b in row]
        self.assertIn("<b>Дела · 2</b>", kwargs["text"])
        self.assertNotIn("INBOX", kwargs["text"])
        self.assertIn("<b>2. Выдать нагрузки</b>", kwargs["text"])
        self.assertIn("<i>BAG · Иванов</i>", kwargs["text"])
        self.assertNotIn("Номер — открыть", kwargs["text"])
        self.assertIn("✓ Готово", labels)
        self.assertIn("task:1", callbacks)
        self.assertIn("nav:all:all:0:qd1", callbacks)
        self.assertEqual(callbacks[-2:], ["nav:today", "nav:secondary"])
        self.assertFalse(any(cb and cb.startswith("nav:all:today") for cb in callbacks))

    def test_task_list_truncates_long_titles_without_hiding_full_card_data(self) -> None:
        full_title = "Очень длинное название задачи " * 10
        rows = [
            {
                "id": 5,
                "title": full_title,
                "project": "TEST",
                "assignee": "—",
                "deadline": None,
            }
        ]
        lines, keyboard = simple_daily._daily_task_lines_and_buttons(rows, ZoneInfo("UTC"))
        text = "\n".join(lines)
        self.assertIn("…</b>", text)
        self.assertNotIn(full_title, text)
        self.assertIn("<i>TEST</i>", text)
        self.assertEqual(keyboard[0][0].callback_data, "task:5")

    async def test_secondary_menu_hides_internal_product_surfaces(self) -> None:
        message = SimpleNamespace(chat=SimpleNamespace(id=1), bot=SimpleNamespace())
        with (
            patch.object(simple_daily.legacy, "_pop_screen_toast", AsyncMock(return_value=None)),
            patch.object(simple_daily, "ui_render", AsyncMock(return_value=7)) as render,
        ):
            await simple_daily.ui_render_home_more(message, _Pool(object()))

        callbacks = [
            b.callback_data
            for row in render.await_args.kwargs["reply_markup"].inline_keyboard
            for b in row
        ]
        self.assertEqual(callbacks, ["nav:projects", "nav:reminders:0", "nav:help", "nav:home"])
        for hidden in ("nav:inbox:0", "nav:work:0", "nav:overdue:0", "home:stats", "sync:status", "nav:team"):
            self.assertNotIn(hidden, callbacks)


if __name__ == "__main__":
    unittest.main()
