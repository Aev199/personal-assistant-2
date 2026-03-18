import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.handlers.projects import cb_project_open


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
    async def fetchrow(self, query, *_args):
        if "SELECT id, code, name FROM projects" in query:
            return {"id": 7, "code": "ABC", "name": "Alpha"}
        raise AssertionError(f"Unexpected fetchrow query: {query}")

    async def fetch(self, query, *_args):
        if "FROM tasks t" in query and "ORDER BY COALESCE(t.parent_task_id, t.id), t.id" in query:
            return [
                {
                    "id": 11,
                    "title": "Root task",
                    "kind": "task",
                    "assignee": "Ира",
                    "deadline": None,
                    "parent_task_id": None,
                    "status": "todo",
                }
            ]
        raise AssertionError(f"Unexpected fetch query: {query}")


class ProjectStructureScreenTests(unittest.IsolatedAsyncioTestCase):
    async def test_project_structure_renders_dedicated_screen(self) -> None:
        callback = SimpleNamespace(
            data="proj:7:structure",
            answer=AsyncMock(),
            bot=SimpleNamespace(),
            from_user=SimpleNamespace(id=1),
            message=SimpleNamespace(chat=SimpleNamespace(id=99)),
        )
        state = AsyncMock()
        deps = SimpleNamespace(admin_id=None, tz_name="Europe/Moscow")
        pool = _Pool(_Conn())

        with (
            patch("bot.handlers.projects.get_current_project_id", AsyncMock(return_value=None)),
            patch("bot.handlers.projects.render_task_tree", return_value=("TREE", [])),
            patch("bot.handlers.projects.ui_render", AsyncMock(return_value=77)) as render,
        ):
            await cb_project_open(callback, state, pool, deps)

        callback.answer.assert_awaited_once()
        state.clear.assert_awaited_once()
        kwargs = render.await_args.kwargs
        self.assertEqual(kwargs["screen"], "project_structure")
        self.assertEqual(kwargs["payload"], {"project_id": 7})
        self.assertIn("ABC", kwargs["text"])
        rows = [[btn.text for btn in row] for row in kwargs["reply_markup"].inline_keyboard]
        self.assertEqual(rows[0], ["⬅ Проект"])
        self.assertEqual(rows[1], ["⬅️ Домой"])


if __name__ == "__main__":
    unittest.main()
