import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.ui.screens import ui_render_all_tasks


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
    async def fetchval(self, query, *_args):
        if "COUNT(*)" in query:
            return 1
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
        raise AssertionError(f"Unexpected fetch query: {query}")


class AllTasksQuickDoneTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_tasks_quick_done_mode_uses_done_callback(self) -> None:
        message = SimpleNamespace(chat=SimpleNamespace(id=201), bot=SimpleNamespace())
        pool = _Pool(_Conn())

        with (
            patch("bot.ui.screens._pop_screen_toast", AsyncMock(return_value=None)),
            patch("bot.ui.screens.ui_render", AsyncMock(return_value=1)) as render,
        ):
            await ui_render_all_tasks(message, pool, tz_name="Europe/Moscow", quick_done=True)

        kwargs = render.await_args.kwargs
        callbacks = [btn.callback_data for row in kwargs["reply_markup"].inline_keyboard for btn in row]
        self.assertIn("task:42:done_quick", callbacks)
        self.assertIn("task:42", callbacks)
        self.assertIn("nav:all:today:qd1", callbacks)

    async def test_all_tasks_default_mode_keeps_open_task_callback(self) -> None:
        message = SimpleNamespace(chat=SimpleNamespace(id=202), bot=SimpleNamespace())
        pool = _Pool(_Conn())

        with (
            patch("bot.ui.screens._pop_screen_toast", AsyncMock(return_value=None)),
            patch("bot.ui.screens.ui_render", AsyncMock(return_value=1)) as render,
        ):
            await ui_render_all_tasks(message, pool, tz_name="Europe/Moscow", quick_done=False)

        kwargs = render.await_args.kwargs
        callbacks = [btn.callback_data for row in kwargs["reply_markup"].inline_keyboard for btn in row]
        self.assertIn("task:42", callbacks)
        self.assertIn("Позвонить клиенту", kwargs["text"])
        self.assertIn("task:42:done_quick", callbacks)


if __name__ == "__main__":
    unittest.main()


class QuickDonePersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_render_keeps_undo_and_navigation_on_same_message(self):
        import time
        from bot.ui.render import ui_render
        undo = {"type": "task_status", "task_id": 42, "prev_status": "in_progress",
                "new_status": "done", "exp": int(time.time()) + 30}
        bot = SimpleNamespace(edit_message_text=AsyncMock(), send_message=AsyncMock(),
                              delete_message=AsyncMock())
        with (
            patch("bot.ui.render.ui_get_state", AsyncMock(return_value={
                "ui_message_id": 17, "ui_screen": "all_tasks", "ui_payload": {"undo": undo}})),
            patch("bot.ui.render.ui_set_state", AsyncMock()) as save,
        ):
            await ui_render(bot=bot, db_pool=_Pool(object()), chat_id=1,
                            text="Рабочие задачи", reply_markup=None, screen="all_tasks",
                            payload={"page": 2, "filter": "nodate"})
        args = bot.edit_message_text.await_args.kwargs
        self.assertEqual(args["message_id"], 17)
        callbacks = [b.callback_data for row in args["reply_markup"].inline_keyboard for b in row]
        self.assertIn("undo:task:42", callbacks)
        self.assertEqual(callbacks[-3:], ["nav:today", "nav:all", "nav:secondary"])
        self.assertEqual(save.await_args.kwargs["ui_payload"],
                         {"page": 2, "filter": "nodate", "undo": undo})
        bot.send_message.assert_not_awaited()

    def test_long_list_keeps_every_task_visible_and_every_action_addressable(self):
        from zoneinfo import ZoneInfo
        from bot.ui.screens import _readable_tasks
        rows = [{"id": n + 100, "project": "Проект", "title": "<&😀>" * 150,
                 "deadline": None} for n in range(30)]
        lines, keyboard = _readable_tasks(rows, ZoneInfo("UTC"))
        text = "\n".join(lines)
        self.assertLess(len(text.encode("utf-16-le")) // 2, 3600)
        self.assertIn("<b>30.</b>", text)
        callbacks = [b.callback_data for row in keyboard for b in row]
        for row in rows:
            self.assertIn(f"task:{row['id']}", callbacks)
            self.assertIn(f"task:{row['id']}:done_quick", callbacks)
