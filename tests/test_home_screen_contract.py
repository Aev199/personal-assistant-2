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
    async def render_home(self, due=(), other=(), reminder=None, payload=None):
        conn = SimpleNamespace(fetch=AsyncMock(side_effect=[list(due), list(other)]),
                               fetchrow=AsyncMock(return_value=reminder))
        with (
            patch("bot.ui.screens._take_screen_payload", AsyncMock(return_value=(payload or {}, None))),
            patch("bot.ui.screens.ui_set_state", AsyncMock()),
            patch("bot.ui.screens.ui_render", AsyncMock(return_value=77)) as render,
        ):
            await ui_render_home(SimpleNamespace(chat=SimpleNamespace(id=11), bot=object()),
                                 _Pool(conn), tz_name="Europe/Moscow")
        return render.await_args.kwargs, conn

    async def test_due_work_leaves_room_for_undated_tasks_and_direct_actions(self):
        due = [task(i, f"Дело {i}", datetime.now(timezone.utc)) for i in range(1, 4)]
        result, conn = await self.render_home(due, [task(4, "Проверить расчёт"), task(5, "Написать письмо")])
        self.assertEqual(result["screen"], "home")
        buttons = result["reply_markup"].inline_keyboard
        self.assertEqual([[b.callback_data for b in row] for row in buttons[:5]],
                         [[f"task:{i}", f"task:{i}:done_quick"] for i in range(1, 6)])
        # Two non-overlapping sets, stable ordering and no current-project filter:
        # a full overdue queue cannot consume the slots reserved for undated work.
        due_call, other_call = conn.fetch.await_args_list
        self.assertIn("t.deadline < $1", due_call.args[0])
        self.assertIn("t.deadline IS NULL OR t.deadline >= $1", other_call.args[0])
        for call in (due_call, other_call):
            self.assertIn("NOT IN ('done','postponed')", call.args[0])
            self.assertIn("t.kind != 'super'", call.args[0])
        self.assertEqual(other_call.args[2], 2)
        self.assertEqual(due_call.args[1], other_call.args[1])
        self.assertEqual(due_call.args[1].hour, 21)  # Midnight Moscow stored as UTC.
        self.assertNotIn("Фокус", result["text"])

    async def test_empty_home_offers_small_list_import(self):
        result, conn = await self.render_home()
        callbacks = [b.callback_data for row in result["reply_markup"].inline_keyboard for b in row]
        self.assertIn("onboard:start", callbacks)
        self.assertIn("nav:add", callbacks)
        self.assertEqual(conn.fetch.await_args_list[1].args[2], 5)
        self.assertNotIn("Inbox: 0", result["text"])

    async def test_reminder_is_visible_and_html_escaped(self):
        result, _ = await self.render_home(reminder={"id": 7, "text": "Позвонить <Ире> & Оле",
                                                    "remind_at": datetime(2026, 9, 6, 7, tzinfo=timezone.utc)})
        self.assertIn("06.09 10:00", result["text"])
        self.assertIn("&lt;Ире&gt; &amp; Оле", result["text"])

    async def test_recent_completion_can_be_undone_from_home(self):
        for seconds, expected in [(30, True), (-1, False)]:
            with self.subTest(seconds=seconds):
                result, _ = await self.render_home(payload={"undo": {
                    "new_status": "done", "task_id": 42,
                    "exp": (datetime.now(timezone.utc) + timedelta(seconds=seconds)).timestamp(),
                }})
                callbacks = [b.callback_data for row in result["reply_markup"].inline_keyboard for b in row]
                self.assertEqual("undo:task:42" in callbacks, expected)

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
        rerender.assert_awaited_once()
        self.assertIs(rerender.await_args.args[0], message)


if __name__ == "__main__":
    unittest.main()
