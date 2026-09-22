import unittest
from unittest.mock import AsyncMock, patch

from bot.services.ideas import archive_idea, promote_idea


class IdeasServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_promote_moves_active_idea_into_inbox_once(self) -> None:
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value={"id": 7, "text": "Попробовать новый отчёт"})
        conn.fetchval = AsyncMock(return_value=123)
        conn.execute = AsyncMock()

        with (
            patch("bot.services.ideas.ensure_inbox_project_id", AsyncMock(return_value=99)) as ensure_inbox,
            patch("bot.services.ideas.db_add_event", AsyncMock()) as add_event,
        ):
            result = await promote_idea(conn, chat_id=42, idea_id=7)

        self.assertEqual(result, (123, "Попробовать новый отчёт"))
        ensure_inbox.assert_awaited_once_with(conn)
        self.assertIn("INSERT INTO tasks", conn.fetchval.await_args.args[0])
        promoted_updates = [
            call for call in conn.execute.await_args_list
            if "status='promoted'" in call.args[0]
        ]
        self.assertEqual(len(promoted_updates), 1)
        add_event.assert_awaited_once()
        self.assertEqual(add_event.await_args.args[1], "idea_promoted")

    async def test_promote_is_idempotent_for_non_active_idea(self) -> None:
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=None)
        conn.fetchval = AsyncMock()
        conn.execute = AsyncMock()

        with (
            patch("bot.services.ideas.ensure_inbox_project_id", AsyncMock()) as ensure_inbox,
            patch("bot.services.ideas.db_add_event", AsyncMock()) as add_event,
        ):
            result = await promote_idea(conn, chat_id=42, idea_id=7)

        self.assertIsNone(result)
        ensure_inbox.assert_not_awaited()
        conn.fetchval.assert_not_awaited()
        add_event.assert_not_awaited()

    async def test_archive_keeps_record_but_removes_it_from_active_state(self) -> None:
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value={"id": 8, "text": "Черновая мысль"})
        conn.execute = AsyncMock()

        with patch("bot.services.ideas.db_add_event", AsyncMock()) as add_event:
            result = await archive_idea(conn, chat_id=42, idea_id=8)

        self.assertEqual(result, "Черновая мысль")
        archived_updates = [
            call for call in conn.execute.await_args_list
            if "status='archived'" in call.args[0]
        ]
        self.assertEqual(len(archived_updates), 1)
        add_event.assert_awaited_once()
        self.assertEqual(add_event.await_args.args[1], "idea_archived")


if __name__ == "__main__":
    unittest.main()
