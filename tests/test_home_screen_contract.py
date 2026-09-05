import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.ui.screens import ui_render_home


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *args):
        return False


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


def task(task_id, title, deadline=None):
    return {"id": task_id, "title": title, "project": "INBOX", "deadline": deadline}


class HomeScreenContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_home_shows_all_work_without_five_task_selection_or_personal_reminders(self):
        tasks = [task(i, f"Дело {i}") for i in range(1, 15)]
        conn = SimpleNamespace(fetch=AsyncMock(return_value=tasks), fetchval=AsyncMock(return_value=14))
        with (
            patch("bot.ui.screens._pop_screen_toast", AsyncMock(return_value=None)),
            patch("bot.ui.screens.ui_render", AsyncMock(return_value=77)) as render,
        ):
            await ui_render_home(SimpleNamespace(chat=SimpleNamespace(id=11), bot=object()),
                                 _Pool(conn), tz_name="Europe/Moscow")
        result = render.await_args.kwargs
        self.assertEqual(result["screen"], "all_tasks")
        self.assertIn("1–14 из 14", result["text"])
        self.assertIn("Дело 14", result["text"])
        self.assertNotIn("Ближайшие", result["text"])
        self.assertNotIn("Напоминание", result["text"])
        callbacks = [b.callback_data for row in result["reply_markup"].inline_keyboard for b in row]
        self.assertIn("task:14", callbacks)
        self.assertIn("nav:all:all:0:qd1", callbacks)
        conn.fetch.assert_awaited_once()

    async def test_quick_completion_keeps_home_and_persists_undo(self):
        from bot.handlers.tasks import cb_task_done_quick

        conn = SimpleNamespace(
            fetchrow=AsyncMock(return_value={"project_id": 1, "project_code": "INBOX",
                                            "title": "Проверить расчёт", "status": "todo"}),
            execute=AsyncMock(),
        )
        message = SimpleNamespace(chat=SimpleNamespace(id=11), bot=object())
        callback = SimpleNamespace(data="task:42:done_quick", message=message, answer=AsyncMock())
        deps = SimpleNamespace(vault=None)
        def close_background(coro, **kwargs):
            coro.close()
        with (
            patch("bot.handlers.tasks._guard", AsyncMock(return_value=True)),
            patch("bot.handlers.tasks.db_add_event", AsyncMock()),
            patch("bot.handlers.tasks.ui_get_state", AsyncMock(return_value={"ui_screen": "home", "ui_payload": {}})),
            patch("bot.handlers.tasks.ui_set_state", AsyncMock()) as persist,
            patch("bot.handlers.tasks.background_project_sync", AsyncMock()),
            patch("bot.handlers.tasks.fire_and_forget", side_effect=close_background),
            patch("bot.handlers.nav._rerender_current_screen", AsyncMock()) as rerender,
        ):
            await cb_task_done_quick(callback, SimpleNamespace(clear=AsyncMock()), _Pool(conn), deps)
        conn.execute.assert_awaited_once_with("UPDATE tasks SET status='done' WHERE id=$1", 42)
        undo = persist.await_args.kwargs["ui_payload"]["undo"]
        self.assertEqual(undo["task_id"], 42)
        self.assertEqual(undo["prev_status"], "todo")
        self.assertEqual(undo["new_status"], "done")
        from bot.ui.state import _undo_active
        self.assertIsNotNone(_undo_active({"undo": undo}, task_id=42))
        rerender.assert_awaited_once()
        self.assertIs(rerender.await_args.args[0], message)


if __name__ == "__main__":
    unittest.main()
