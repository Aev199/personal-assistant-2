import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.handlers.projects import cb_project_open
from bot.handlers.system import cb_global_tails_pick
from bot.handlers.tasks import show_super_task_card


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


class _ProjectTailsConn:
    async def fetchrow(self, query, *_args):
        if "SELECT id, code, name FROM projects" in query:
            return {"id": 7, "code": "ABC", "name": "Alpha"}
        raise AssertionError(f"Unexpected fetchrow query: {query}")

    async def fetchval(self, query, *_args):
        if "SELECT COUNT(*) FROM tasks t WHERE t.project_id=$1" in query:
            return 1
        raise AssertionError(f"Unexpected fetchval query: {query}")

    async def fetch(self, query, *_args):
        if "SELECT t.id, t.title, COALESCE(tm.name,'—') AS assignee, t.deadline" in query:
            return [
                {
                    "id": 11,
                    "title": "Tail task",
                    "assignee": "Ира",
                    "deadline": None,
                }
            ]
        raise AssertionError(f"Unexpected fetch query: {query}")


class _GlobalTailsConn:
    async def fetchval(self, query, *_args):
        if "SELECT COUNT(*) FROM tasks t WHERE t.status != 'done'" in query:
            return 1
        raise AssertionError(f"Unexpected fetchval query: {query}")

    async def fetch(self, query, *_args):
        if "SELECT t.id, t.title, p.code AS project" in query:
            return [
                {
                    "id": 12,
                    "title": "Global tail",
                    "project": "OPS",
                    "assignee": "Оля",
                    "deadline": None,
                }
            ]
        raise AssertionError(f"Unexpected fetch query: {query}")


class _SuperConn:
    async def fetchrow(self, query, *_args):
        if "FROM tasks t" in query and "JOIN projects p" in query and "WHERE t.id=$1" in query:
            return {
                "id": 30,
                "title": "Epic",
                "status": "todo",
                "kind": "super",
                "project_id": 7,
                "project_code": "ABC",
            }
        if "COUNT(*) AS total" in query:
            return {"total": 2, "done": 1, "active": 1}
        raise AssertionError(f"Unexpected fetchrow query: {query}")

    async def fetch(self, query, *_args):
        if "WHERE t.parent_task_id=$1" in query:
            return [
                {
                    "id": 31,
                    "title": "Child task",
                    "status": "todo",
                    "deadline": None,
                    "assignee": "Ира",
                }
            ]
        raise AssertionError(f"Unexpected fetch query: {query}")


class DeepNavigationContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_project_tails_pick_is_action_ready(self) -> None:
        callback = SimpleNamespace(
            data="proj:7:tails_pick:nodate:0",
            answer=AsyncMock(),
            bot=SimpleNamespace(),
            from_user=SimpleNamespace(id=5),
            message=SimpleNamespace(chat=SimpleNamespace(id=99)),
        )
        state = AsyncMock()
        deps = SimpleNamespace(admin_id=None, tz_name="Europe/Moscow")
        pool = _Pool(_ProjectTailsConn())

        with (
            patch("bot.handlers.projects.get_current_project_id", AsyncMock(return_value=None)),
            patch("bot.handlers.projects.ui_render", AsyncMock(return_value=77)) as render,
        ):
            await cb_project_open(callback, state, pool, deps)

        kwargs = render.await_args.kwargs
        self.assertEqual(kwargs["screen"], "project_tails_pick")
        self.assertNotIn("Tail task", kwargs["text"])
        first_button = kwargs["reply_markup"].inline_keyboard[0][0].text
        self.assertIn("Tail task", first_button)
        self.assertIn("без срока", first_button)

    async def test_global_tails_pick_is_action_ready(self) -> None:
        callback = SimpleNamespace(
            data="nav:tails_pick:nodate:0:nav:projects",
            answer=AsyncMock(),
            bot=SimpleNamespace(),
            from_user=SimpleNamespace(id=1),
            message=SimpleNamespace(chat=SimpleNamespace(id=42)),
        )
        state = AsyncMock()
        deps = SimpleNamespace(admin_id=1, tz_name="Europe/Moscow")
        pool = _Pool(_GlobalTailsConn())

        with patch("bot.handlers.system.ui_render", AsyncMock(return_value=77)) as render:
            await cb_global_tails_pick(callback, state, pool, deps)

        kwargs = render.await_args.kwargs
        self.assertEqual(kwargs["screen"], "tails_pick")
        self.assertNotIn("Global tail", kwargs["text"])
        first_button = kwargs["reply_markup"].inline_keyboard[0][0].text
        self.assertIn("Global tail", first_button)
        self.assertIn("[OPS]", first_button)

    async def test_super_task_card_is_action_ready(self) -> None:
        message = SimpleNamespace(chat=SimpleNamespace(id=77), bot=SimpleNamespace())
        pool = _Pool(_SuperConn())
        deps = SimpleNamespace(tz_name="Europe/Moscow")

        with (
            patch("bot.handlers.tasks.ui_get_state", AsyncMock(return_value={"ui_screen": "project_card", "ui_payload": {"project_id": 7, "page": 0}})),
            patch("bot.handlers.tasks.ui_render", AsyncMock(return_value=77)) as render,
        ):
            await show_super_task_card(message, pool, 30, deps=deps, page=0)

        kwargs = render.await_args.kwargs
        self.assertEqual(kwargs["screen"], "super_task")
        self.assertNotIn("Child task", kwargs["text"])
        child_row = kwargs["reply_markup"].inline_keyboard[1][0].text
        self.assertIn("Child task", child_row)
        self.assertIn("без срока", child_row)


if __name__ == "__main__":
    unittest.main()
