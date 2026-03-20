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
        if "COUNT(*) FILTER (WHERE status != 'done' AND kind != 'super') AS active" in query:
            return {"active": 1, "overdue": 0}
        raise AssertionError(f"Unexpected fetchrow query: {query}")

    async def fetchval(self, query, *_args):
        if "SELECT COUNT(*) FROM tasks WHERE project_id=$1 AND parent_task_id IS NULL AND status != 'done'" in query:
            return 1
        raise AssertionError(f"Unexpected fetchval query: {query}")

    async def fetch(self, query, *_args):
        if "WHERE project_id=$1 AND parent_task_id IS NULL AND status != 'done'" in query:
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
    async def test_project_card_is_action_first(self) -> None:
        callback = SimpleNamespace(
            data="proj:7",
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
            patch("bot.handlers.projects.ui_render", AsyncMock(return_value=77)) as render,
        ):
            await cb_project_open(callback, state, pool, deps)

        kwargs = render.await_args.kwargs
        self.assertEqual(kwargs["screen"], "project_card")
        rows = [[btn.text for btn in row] for row in kwargs["reply_markup"].inline_keyboard]
        self.assertEqual(rows[0], ["Root task"])
        self.assertEqual(rows[1], ["➕ Задача", "🧩 Суперзадача"])
        self.assertEqual(rows[2], ["⋯ Ещё"])
        self.assertEqual(rows[-1], ["⬅️ Проекты", "⬅️ Домой"])
        labels = [label for row in rows for label in row]
        self.assertNotIn("🗂 Структура", labels)
        self.assertNotIn("🧺 Хвосты", labels)
        self.assertNotIn("📦 В архив", labels)
        self.assertNotIn("Сделать текущим", labels)

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
        self.assertEqual(rows[0], ["⬅ Ещё"])
        self.assertEqual(rows[1], ["⬅️ Домой"])

    async def test_project_more_renders_secondary_actions_only(self) -> None:
        callback = SimpleNamespace(
            data="proj:7:more:0",
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
            patch("bot.handlers.projects.ui_render", AsyncMock(return_value=77)) as render,
        ):
            await cb_project_open(callback, state, pool, deps)

        kwargs = render.await_args.kwargs
        self.assertEqual(kwargs["screen"], "project_more")
        self.assertEqual(kwargs["payload"], {"project_id": 7, "page": 0})
        self.assertIn("Дополнительные действия проекта", kwargs["text"])
        rows = [[btn.text for btn in row] for row in kwargs["reply_markup"].inline_keyboard]
        self.assertEqual(rows[0], ["🗂 Структура", "🧺 Хвосты"])
        self.assertEqual(rows[1], ["Сделать текущим"])
        self.assertEqual(rows[2], ["📦 В архив"])
        self.assertEqual(rows[3], ["⬅ Проект", "⬅️ Домой"])


if __name__ == "__main__":
    unittest.main()
