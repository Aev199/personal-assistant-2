import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.ui.screens import ui_render_help, ui_render_home_more, ui_render_stats, ui_render_team


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


class _StatsConn:
    async def fetchval(self, query, *_args):
        if "SELECT p.code FROM projects p" in query:
            return "ABC"
        if "SELECT id FROM projects WHERE code='INBOX'" in query:
            return 99
        if "deadline IS NOT NULL AND deadline <" in query:
            return 5
        if "deadline IS NULL AND project_id !=" in query:
            return 3
        if "(deadline AT TIME ZONE 'UTC' AT TIME ZONE $1)::date" in query:
            return 2
        if "project_id=$1" in query and "COUNT(*) FROM tasks" in query:
            return 4
        if "SELECT COUNT(*) FROM projects" in query:
            return 7
        if "SELECT COUNT(*) FROM tasks WHERE status != 'done' AND kind != 'super'" in query:
            return 12
        if "SELECT text FROM reminders" in query:
            return "Review PR"
        raise AssertionError(f"Unexpected fetchval query: {query}")

    async def fetchrow(self, query, *_args):
        if "FROM sync_status" in query:
            return None
        raise AssertionError(f"Unexpected fetchrow query: {query}")

    async def execute(self, *_args, **_kwargs):
        return None


class _TeamConn:
    async def fetch(self, query, *_args):
        if "SELECT id, name, role FROM team" in query:
            return [
                {"id": 1, "name": "Ира", "role": "pm"},
                {"id": 2, "name": "Оля", "role": "dev"},
                {"id": 3, "name": "Саша", "role": "qa"},
            ]
        if "SELECT assignee_id, deadline FROM tasks" in query:
            return [
                {"assignee_id": 1, "deadline": None},
                {"assignee_id": 2, "deadline": None},
                {"assignee_id": 3, "deadline": None},
            ]
        raise AssertionError(f"Unexpected fetch query: {query}")


def _callbacks(markup) -> list[list[str]]:
    return [[btn.callback_data for btn in row] for row in markup.inline_keyboard]


class SecondarySurfacesContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_secondary_menu_keeps_secondary_destinations_compact(self) -> None:
        message = SimpleNamespace(chat=SimpleNamespace(id=11), bot=SimpleNamespace())

        with (
            patch("bot.ui.screens._pop_screen_toast", AsyncMock(return_value=None)),
            patch("bot.ui.screens.ui_render", AsyncMock(return_value=77)) as render,
        ):
            await ui_render_home_more(message, db_pool=object())

        rows = _callbacks(render.await_args.kwargs["reply_markup"])
        self.assertEqual(rows[0], ["nav:all", "nav:reminders:0"])
        self.assertEqual(rows[1], ["home:stats", "sync:status"])
        self.assertEqual(rows[2], ["nav:help", "nav:team"])
        self.assertEqual(rows[3], ["nav:home"])

    async def test_help_screen_has_fast_escape_routes(self) -> None:
        message = SimpleNamespace(chat=SimpleNamespace(id=12), bot=SimpleNamespace())

        with (
            patch("bot.ui.screens._pop_screen_toast", AsyncMock(return_value=None)),
            patch("bot.ui.screens.ui_render", AsyncMock(return_value=77)) as render,
        ):
            await ui_render_help(message, db_pool=object())

        text = render.await_args.kwargs["text"]
        self.assertIn("/help", text)
        rows = _callbacks(render.await_args.kwargs["reply_markup"])
        self.assertEqual(rows[0], ["nav:today", "nav:add"])
        self.assertEqual(rows[1], ["nav:secondary", "nav:home"])

    async def test_stats_screen_is_secondary_not_daily_action_hub(self) -> None:
        message = SimpleNamespace(chat=SimpleNamespace(id=13), bot=SimpleNamespace())
        pool = _Pool(_StatsConn())

        with (
            patch("bot.ui.screens._take_screen_payload", AsyncMock(return_value=({}, None))),
            patch("bot.ui.screens.ui_set_state", AsyncMock()),
            patch("bot.ui.screens.ui_render", AsyncMock(return_value=77)) as render,
        ):
            await ui_render_stats(message, pool, tz_name="Europe/Moscow")

        rows = _callbacks(render.await_args.kwargs["reply_markup"])
        self.assertEqual(rows[0], ["home:stats", "sync:status"])
        self.assertEqual(rows[1], ["nav:secondary", "nav:home"])

    async def test_team_screen_moves_member_details_into_buttons(self) -> None:
        message = SimpleNamespace(chat=SimpleNamespace(id=14), bot=SimpleNamespace())
        pool = _Pool(_TeamConn())

        with (
            patch("bot.ui.screens._pop_screen_toast", AsyncMock(return_value=None)),
            patch("bot.ui.screens.ui_render", AsyncMock(return_value=77)) as render,
        ):
            await ui_render_team(message, pool)

        kwargs = render.await_args.kwargs
        markup = kwargs["reply_markup"]
        rows = _callbacks(markup)
        buttons = markup.inline_keyboard
        self.assertTrue(buttons[0][0].text.startswith("👤 Ира · "))
        self.assertTrue(buttons[0][1].text.startswith("👤 Оля · "))
        self.assertTrue(buttons[1][0].text.startswith("👤 Саша · "))
        self.assertNotIn("активно", buttons[0][0].text)
        self.assertEqual(rows[0], ["team:1:0", "team:2:0"])
        self.assertEqual(rows[1], ["team:3:0"])
        self.assertEqual(rows[-2], ["team:add"])
        self.assertEqual(rows[-1], ["nav:home"])


if __name__ == "__main__":
    unittest.main()
