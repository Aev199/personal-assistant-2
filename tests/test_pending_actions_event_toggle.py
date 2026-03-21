import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.handlers.pending_actions import cb_llm_toggle_event_kind
from bot.services.pending_actions import create_pending_preview


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


class PendingActionsEventToggleTests(unittest.IsolatedAsyncioTestCase):
    async def test_event_preview_shows_kind_toggle_button(self) -> None:
        message = SimpleNamespace(chat=SimpleNamespace(id=10), answer=AsyncMock())
        deps = SimpleNamespace(tz_name="Europe/Moscow")

        with (
            patch("bot.services.pending_actions.create_pending_action", AsyncMock(return_value=55)),
            patch("bot.services.pending_actions.remember_recent_action", AsyncMock()),
        ):
            pending_id = await create_pending_preview(
                message,
                db_pool=_Pool(SimpleNamespace()),
                deps=deps,
                kind="event",
                payload={
                    "title": "1:1",
                    "calendar_kind": "personal",
                    "calendar_url": "personal://calendar",
                    "summary": "1:1",
                    "start_local": "2026-03-22T10:00:00+03:00",
                    "duration_min": 30,
                },
                fingerprint="fingerprint",
                summary="1:1",
                source="text",
            )

        self.assertEqual(pending_id, 55)
        markup = message.answer.await_args.kwargs["reply_markup"]
        rows = [[btn.text for btn in row] for row in markup.inline_keyboard]
        self.assertEqual(rows[0], ["✅ Подтвердить", "✖ Отмена"])
        self.assertEqual(rows[1], ["💼 Рабочее"])
        self.assertEqual(
            markup.inline_keyboard[1][0].callback_data,
            "llm:toggle_event_kind:55",
        )

    async def test_toggle_event_kind_switches_personal_to_work_and_defaults_inbox(self) -> None:
        message = SimpleNamespace(chat=SimpleNamespace(id=11), edit_text=AsyncMock())
        callback = SimpleNamespace(data="llm:toggle_event_kind:7", message=message, answer=AsyncMock())
        deps = SimpleNamespace(tz_name="Europe/Moscow")
        pending = {
            "id": 7,
            "kind": "event",
            "status": "pending",
            "payload": {
                "title": "1:1",
                "calendar_kind": "personal",
                "calendar_url": "personal://calendar",
                "summary": "1:1",
                "start_local": "2026-03-22T10:00:00+03:00",
                "duration_min": 30,
            },
        }

        with (
            patch("bot.handlers.pending_actions.get_pending_action", AsyncMock(return_value=pending)),
            patch("bot.handlers.pending_actions.update_pending_action_payload", AsyncMock()) as update_payload,
            patch("bot.handlers.pending_actions.ensure_inbox_project_id", AsyncMock(return_value=99)),
            patch.dict(
                "bot.handlers.pending_actions.os.environ",
                {
                    "ICLOUD_CALENDAR_URL_WORK": "work://calendar",
                    "ICLOUD_CALENDAR_URL_PERSONAL": "personal://calendar",
                },
                clear=False,
            ),
        ):
            await cb_llm_toggle_event_kind(callback, _Pool(SimpleNamespace()), deps)

        update_payload.assert_awaited_once()
        updated = update_payload.await_args.kwargs["payload"]
        self.assertEqual(updated["calendar_kind"], "work")
        self.assertEqual(updated["calendar_url"], "work://calendar")
        self.assertEqual(updated["project_id"], 99)
        self.assertEqual(updated["project_code"], "INBOX")
        self.assertEqual(updated["summary"], "INBOX: 1:1")
        message.edit_text.assert_awaited_once()
        self.assertIn("Календарь: рабочий", message.edit_text.await_args.args[0])
        callback.answer.assert_awaited()

    async def test_toggle_event_kind_switches_work_to_personal_and_clears_project(self) -> None:
        message = SimpleNamespace(chat=SimpleNamespace(id=12), edit_text=AsyncMock())
        callback = SimpleNamespace(data="llm:toggle_event_kind:8", message=message, answer=AsyncMock())
        deps = SimpleNamespace(tz_name="Europe/Moscow")
        pending = {
            "id": 8,
            "kind": "event",
            "status": "pending",
            "payload": {
                "title": "Standup",
                "calendar_kind": "work",
                "calendar_url": "work://calendar",
                "project_id": 17,
                "project_code": "OPS",
                "project_name": "Operations",
                "summary": "OPS: Standup",
                "start_local": "2026-03-22T10:00:00+03:00",
                "duration_min": 30,
            },
        }

        with (
            patch("bot.handlers.pending_actions.get_pending_action", AsyncMock(return_value=pending)),
            patch("bot.handlers.pending_actions.update_pending_action_payload", AsyncMock()) as update_payload,
            patch.dict(
                "bot.handlers.pending_actions.os.environ",
                {
                    "ICLOUD_CALENDAR_URL_WORK": "work://calendar",
                    "ICLOUD_CALENDAR_URL_PERSONAL": "personal://calendar",
                },
                clear=False,
            ),
        ):
            await cb_llm_toggle_event_kind(callback, _Pool(SimpleNamespace()), deps)

        update_payload.assert_awaited_once()
        updated = update_payload.await_args.kwargs["payload"]
        self.assertEqual(updated["calendar_kind"], "personal")
        self.assertEqual(updated["calendar_url"], "personal://calendar")
        self.assertNotIn("project_id", updated)
        self.assertNotIn("project_code", updated)
        self.assertNotIn("project_name", updated)
        self.assertEqual(updated["summary"], "Standup")
        message.edit_text.assert_awaited_once()
        self.assertIn("Календарь: личный", message.edit_text.await_args.args[0])
        callback.answer.assert_awaited()


if __name__ == "__main__":
    unittest.main()
