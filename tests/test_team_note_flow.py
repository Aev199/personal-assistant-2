import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.handlers.team import cb_team_member_details, cb_team_note_edit, msg_team_note_save


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


class _CardConn:
    async def fetchrow(self, query, *_args):
        if "SELECT name, role, note FROM team" in query:
            return {"name": "Ира", "role": "pm", "note": "Любит короткие синки"}
        raise AssertionError(f"Unexpected fetchrow query: {query}")

    async def fetchval(self, query, *_args):
        if "COUNT(*) FROM tasks WHERE assignee_id = $1 AND status != 'done'" in query:
            return 0
        if "COUNT(*) FROM tasks WHERE assignee_id = $1 AND status != 'done' AND deadline IS NOT NULL" in query:
            return 0
        raise AssertionError(f"Unexpected fetchval query: {query}")

    async def fetch(self, query, *_args):
        if "FROM tasks t" in query:
            return []
        raise AssertionError(f"Unexpected fetch query: {query}")


class _EditConn:
    async def fetchrow(self, query, *_args):
        if "SELECT name, note FROM team WHERE id=$1" in query:
            return {"name": "Ира", "note": "Любит короткие синки"}
        raise AssertionError(f"Unexpected fetchrow query: {query}")


class _SaveConn:
    def __init__(self):
        self.execute = AsyncMock()


class TeamNoteFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_team_card_displays_note_and_edit_button(self) -> None:
        callback = SimpleNamespace(
            data="team:7:0",
            answer=AsyncMock(),
            from_user=SimpleNamespace(id=1),
            bot=SimpleNamespace(),
            message=SimpleNamespace(chat=SimpleNamespace(id=55)),
        )
        state = AsyncMock()
        deps = SimpleNamespace(admin_id=1)
        pool = _Pool(_CardConn())

        with patch("bot.handlers.team.ui_render", AsyncMock(return_value=77)) as render:
            await cb_team_member_details(callback, state, pool, deps)

        kwargs = render.await_args.kwargs
        self.assertIn("Любит короткие синки", kwargs["text"])
        rows = [[btn.text for btn in row] for row in kwargs["reply_markup"].inline_keyboard]
        self.assertEqual(rows[0], ["📝 Редактировать заметку"])

    async def test_note_edit_prompt_shows_clear_when_note_exists(self) -> None:
        callback = SimpleNamespace(
            data="teamnote:edit:7:2",
            answer=AsyncMock(),
            from_user=SimpleNamespace(id=1),
            bot=SimpleNamespace(),
            message=SimpleNamespace(chat=SimpleNamespace(id=55), message_id=99),
        )
        state = AsyncMock()
        deps = SimpleNamespace(admin_id=1)
        pool = _Pool(_EditConn())

        with patch("bot.handlers.team.wizard_render", AsyncMock()) as render:
            await cb_team_note_edit(callback, state, pool, deps)

        state.set_state.assert_awaited_once()
        rows = [[btn.text for btn in row] for row in render.await_args.kwargs["reply_markup"].inline_keyboard]
        self.assertEqual(rows[0], ["🗑 Очистить заметку"])
        self.assertEqual(rows[1], ["⬅ Назад", "✖️ Отмена"])

    async def test_note_save_updates_team_and_rerenders_card(self) -> None:
        message = SimpleNamespace(
            text="Созваниваться вечером",
            chat=SimpleNamespace(id=77),
            from_user=SimpleNamespace(id=1),
            bot=SimpleNamespace(),
        )
        state = AsyncMock()
        state.get_data = AsyncMock(return_value={"team_note_emp_id": 7, "team_note_page": 1, "team_note_has_note": True})
        deps = SimpleNamespace(admin_id=1)
        conn = _SaveConn()
        pool = _Pool(conn)

        with (
            patch("bot.handlers.team.escape_hatch_menu_or_command", AsyncMock(return_value=False)),
            patch("bot.handlers.team.try_delete_user_message", AsyncMock()),
            patch("bot.handlers.team.ui_get_state", AsyncMock(return_value={"ui_payload": {}})),
            patch("bot.handlers.team.ui_set_state", AsyncMock()),
            patch("bot.handlers.team.ui_render_team_member_from_message", AsyncMock()) as render_card,
        ):
            await msg_team_note_save(message, state, pool, deps)

        conn.execute.assert_awaited_once_with("UPDATE team SET note=$2 WHERE id=$1", 7, "Созваниваться вечером")
        render_card.assert_awaited_once_with(message, pool, deps, emp_id=7, page=1)


if __name__ == "__main__":
    unittest.main()
