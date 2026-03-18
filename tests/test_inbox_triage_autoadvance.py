import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.handlers.tasks import _advance_inbox_triage_after_action


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
    def __init__(self, next_row):
        self.next_row = next_row

    async def fetchrow(self, query, *_args):
        if "SELECT id, created_at" in query:
            return self.next_row
        raise AssertionError(f"Unexpected query: {query}")


class InboxTriageAutoAdvanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_advances_to_next_inbox_task(self) -> None:
        next_created_at = datetime(2026, 3, 18, 10, 30, tzinfo=timezone.utc)
        conn = _Conn({"id": 22, "created_at": next_created_at})
        pool = _Pool(conn)
        message = SimpleNamespace(chat=SimpleNamespace(id=101))
        deps = SimpleNamespace(tz_name="Europe/Moscow")
        payload = {
            "triage": {
                "active": True,
                "mode": "inbox",
                "anchor_id": 11,
                "anchor_created_at": "2026-03-18T10:00:00+00:00",
                "inbox_id": 7,
                "return": "inbox",
            }
        }

        with (
            patch("bot.handlers.tasks.ui_get_state", AsyncMock(return_value={"ui_payload": payload})),
            patch("bot.handlers.tasks.ui_set_state", AsyncMock()) as set_state,
            patch("bot.handlers.tasks.show_task_card", AsyncMock()) as show_task_card,
        ):
            advanced = await _advance_inbox_triage_after_action(message, pool, deps, task_id=11)

        self.assertTrue(advanced)
        show_task_card.assert_awaited_once_with(message, pool, 22, deps=deps)
        kwargs = set_state.await_args.kwargs
        self.assertEqual(kwargs["ui_screen"], "inbox_triage")
        self.assertEqual(kwargs["ui_payload"]["triage"]["anchor_id"], 22)
        self.assertEqual(kwargs["ui_payload"]["triage"]["anchor_created_at"], next_created_at.isoformat())

    async def test_returns_home_with_toast_when_triage_finished(self) -> None:
        conn = _Conn(None)
        pool = _Pool(conn)
        message = SimpleNamespace(chat=SimpleNamespace(id=202))
        deps = SimpleNamespace(tz_name="Europe/Moscow")
        payload = {
            "triage": {
                "active": True,
                "mode": "inbox",
                "anchor_id": 11,
                "anchor_created_at": "2026-03-18T10:00:00+00:00",
                "inbox_id": 7,
                "return": "home",
            }
        }

        with (
            patch("bot.handlers.tasks.ui_get_state", AsyncMock(return_value={"ui_payload": payload})),
            patch("bot.handlers.tasks.ui_set_state", AsyncMock()) as set_state,
            patch("bot.ui.screens.ui_render_home", AsyncMock()) as render_home,
            patch("bot.ui.screens.ui_render_inbox", AsyncMock()) as render_inbox,
        ):
            advanced = await _advance_inbox_triage_after_action(message, pool, deps, task_id=11)

        self.assertTrue(advanced)
        render_home.assert_awaited_once_with(message, pool, tz_name="Europe/Moscow")
        render_inbox.assert_not_awaited()
        kwargs = set_state.await_args.kwargs
        self.assertNotIn("triage", kwargs["ui_payload"])
        self.assertEqual(kwargs["ui_payload"]["toast"]["text"], "🎉 Inbox разобран")


if __name__ == "__main__":
    unittest.main()
