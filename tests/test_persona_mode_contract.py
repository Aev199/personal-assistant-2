import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.db.user_settings import get_persona_mode, set_persona_mode
from bot.handlers.nav import cb_settings_persona
from bot.handlers.tasks import build_task_card
from bot.ui.screens import ui_render_team


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
        if "FROM tasks t" in query and "WHERE t.id=$1" in query:
            return {
                "id": 10,
                "title": "Подготовить отчёт",
                "status": "todo",
                "kind": "task",
                "deadline": None,
                "parent_task_id": None,
                "g_task_id": None,
                "g_task_list_id": None,
                "g_task_hash": None,
                "project_id": 5,
                "project_code": "OPS",
                "assignee": "Ира",
            }
        raise AssertionError(f"Unexpected fetchrow query: {query}")

    async def fetch(self, query, *_args):
        if "SELECT id, title, status FROM tasks WHERE parent_task_id=$1" in query:
            return []
        raise AssertionError(f"Unexpected fetch query: {query}")


class PersonaModeContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_user_settings_persona_defaults_to_lead(self) -> None:
        conn = SimpleNamespace(fetchval=AsyncMock(return_value=None))
        self.assertEqual(await get_persona_mode(conn, 1), "lead")

    async def test_set_persona_mode_normalizes_value(self) -> None:
        conn = SimpleNamespace(execute=AsyncMock())
        result = await set_persona_mode(conn, 1, "SOLO")
        self.assertEqual(result, "solo")
        conn.execute.assert_awaited_once()

    async def test_team_screen_redirects_to_secondary_in_solo(self) -> None:
        message = SimpleNamespace(chat=SimpleNamespace(id=55), bot=SimpleNamespace())
        pool = _Pool(object())

        with (
            patch("bot.ui.screens.get_persona_mode", AsyncMock(return_value="solo")),
            patch("bot.ui.screens.ui_get_state", AsyncMock(return_value={"ui_payload": {}})),
            patch("bot.ui.screens.ui_set_state", AsyncMock()),
            patch("bot.ui.screens.ui_render_home_more", AsyncMock(return_value=77)) as render_secondary,
        ):
            result = await ui_render_team(message, pool)

        self.assertEqual(result, 77)
        render_secondary.assert_awaited_once()

    async def test_task_card_text_hides_assignee_in_solo(self) -> None:
        deps = SimpleNamespace(tz_name="Europe/Moscow")
        pool = _Pool(_TaskConn())

        with (
            patch("bot.handlers.tasks.get_persona_mode", AsyncMock(return_value="solo")),
            patch("bot.handlers.tasks.ui_get_state", AsyncMock(return_value={"ui_screen": "work", "ui_payload": {}})),
        ):
            text, kb, kind = await build_task_card(
                db_pool=pool,
                chat_id=77,
                task_id=10,
                deps=deps,
                expanded=False,
            )

        self.assertEqual(kind, "task")
        self.assertNotIn("Исполнитель:", text)
        labels = [btn.text for row in kb.inline_keyboard for btn in row]
        self.assertNotIn("👤 Исп.", labels)

    async def test_persona_switch_refreshes_menu_and_rerenders_current_screen(self) -> None:
        callback = SimpleNamespace(
            data="settings:persona:solo",
            answer=AsyncMock(),
            from_user=SimpleNamespace(id=1),
            message=SimpleNamespace(chat=SimpleNamespace(id=77), bot=SimpleNamespace()),
            bot=SimpleNamespace(),
        )
        state = AsyncMock()
        deps = SimpleNamespace(admin_id=1, tz_name="Europe/Moscow", icloud=None)
        pool = _Pool(object())

        with (
            patch("bot.handlers.nav._callback_wizard_context", AsyncMock(return_value=(None, None, None))),
            patch("bot.handlers.nav.set_persona_mode", AsyncMock(return_value="solo")),
            patch("bot.handlers.nav.ensure_main_menu", AsyncMock(return_value=False)) as ensure_menu,
            patch("bot.handlers.nav._rerender_current_screen", AsyncMock(return_value=91)) as rerender,
            patch("bot.handlers.nav.cleanup_stale_wizard_message", AsyncMock()),
        ):
            await cb_settings_persona(callback, state, pool, deps)

        callback.answer.assert_awaited_once()
        ensure_menu.assert_awaited_once_with(callback.message, pool, refresh=True)
        self.assertEqual(rerender.await_args.kwargs["persona_mode"], "solo")
        self.assertIn("Solo", rerender.await_args.kwargs["toast"])


if __name__ == "__main__":
    unittest.main()
