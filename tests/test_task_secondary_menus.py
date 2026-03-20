import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.handlers.tasks import cb_task


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


class _TaskConn:
    async def fetchrow(self, query, *_args):
        if "SELECT t.id, t.title, t.status, t.deadline, t.project_id, t.parent_task_id" in query:
            return {
                "id": 10,
                "title": "Parent task",
                "status": "todo",
                "deadline": None,
                "project_id": 20,
                "parent_task_id": 30,
                "g_task_id": None,
                "g_task_list_id": None,
                "g_task_hash": None,
                "project_code": "ABC",
                "assignee": "Ира",
            }
        if "SELECT t.id, t.title, p.code AS project_code" in query:
            return {"id": 10, "title": "Parent task", "project_code": "ABC"}
        raise AssertionError(f"Unexpected fetchrow query: {query}")

    async def fetch(self, query, *_args):
        if "SELECT id, title, status FROM tasks WHERE parent_task_id=$1 AND status != 'done' ORDER BY id" in query:
            return [
                {"id": 41, "title": "Child A", "status": "todo"},
                {"id": 42, "title": "Child B", "status": "in_progress"},
            ]
        raise AssertionError(f"Unexpected fetch query: {query}")


class TaskSecondaryMenuTests(unittest.IsolatedAsyncioTestCase):
    async def test_relations_menu_is_compact_and_routes_into_deeper_actions(self) -> None:
        callback = SimpleNamespace(
            data="task:10:relations",
            answer=AsyncMock(),
            bot=SimpleNamespace(),
            from_user=SimpleNamespace(id=1),
            message=SimpleNamespace(chat=SimpleNamespace(id=99), bot=SimpleNamespace()),
        )
        state = AsyncMock()
        deps = SimpleNamespace(
            admin_id=None,
            tz_name="Europe/Moscow",
            vault=SimpleNamespace(),
            gtasks=SimpleNamespace(enabled=lambda: True),
        )
        pool = _Pool(_TaskConn())

        with patch("bot.handlers.tasks.ui_render", AsyncMock(return_value=77)) as render:
            await cb_task(callback, state, pool, deps)

        kwargs = render.await_args.kwargs
        self.assertEqual(kwargs["screen"], "task_relations")
        self.assertIn("Структурные действия вынесены сюда", kwargs["text"])
        rows = [[btn.text for btn in row] for row in kwargs["reply_markup"].inline_keyboard]
        self.assertEqual(rows[0], ["📁 В проект…"])
        self.assertEqual(rows[1], ["🧩 В суперзадачу…", "⛓ Отвязать"])
        self.assertEqual(rows[2], ["📤 В Google Tasks"])
        self.assertEqual(rows[3], ["⬅ Назад", "⬅️ Домой"])

    async def test_subtasks_menu_lists_children_without_overloading_primary_card(self) -> None:
        callback = SimpleNamespace(
            data="task:10:subtasks",
            answer=AsyncMock(),
            bot=SimpleNamespace(),
            from_user=SimpleNamespace(id=1),
            message=SimpleNamespace(chat=SimpleNamespace(id=99), bot=SimpleNamespace()),
        )
        state = AsyncMock()
        deps = SimpleNamespace(
            admin_id=None,
            tz_name="Europe/Moscow",
            vault=SimpleNamespace(),
            gtasks=SimpleNamespace(enabled=lambda: True),
        )
        pool = _Pool(_TaskConn())

        with patch("bot.handlers.tasks.ui_render", AsyncMock(return_value=77)) as render:
            await cb_task(callback, state, pool, deps)

        kwargs = render.await_args.kwargs
        self.assertEqual(kwargs["screen"], "task_subtasks")
        self.assertIn("Подзадачи", kwargs["text"])
        rows = [[btn.text for btn in row] for row in kwargs["reply_markup"].inline_keyboard]
        self.assertEqual(rows[0], ["↳ Child A"])
        self.assertEqual(rows[1], ["↳ Child B"])
        self.assertEqual(rows[-2], ["⬅ Назад"])
        self.assertEqual(rows[-1], ["⬅️ Домой"])


if __name__ == "__main__":
    unittest.main()
