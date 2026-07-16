import unittest
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import bot.services.product_mode as product_mode
from bot.services.product_mode import (
    SAFE_INSTANT_KINDS,
    _action_word,
    _evening_due,
    _morning_due,
)


class _Acquire:
    def __init__(self, conn) -> None:
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class _Pool:
    def __init__(self, conn=None) -> None:
        self.conn = conn or SimpleNamespace()

    def acquire(self):
        return _Acquire(self.conn)


class ProductModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tz = ZoneInfo("Europe/Moscow")

    def test_safe_instant_kinds_exclude_calendar_events(self) -> None:
        self.assertEqual(
            SAFE_INSTANT_KINDS,
            {"task", "personal_task", "reminder", "idea"},
        )
        self.assertNotIn("event", SAFE_INSTANT_KINDS)

    def test_morning_brief_is_once_per_day_inside_window(self) -> None:
        now = datetime(2026, 7, 16, 8, 30, tzinfo=self.tz)
        self.assertTrue(
            _morning_due(
                now,
                last_sent=date(2026, 7, 15),
                morning_hour=8,
                evening_hour=19,
            )
        )
        self.assertFalse(
            _morning_due(
                now,
                last_sent=date(2026, 7, 16),
                morning_hour=8,
                evening_hour=19,
            )
        )
        self.assertFalse(
            _morning_due(
                now.replace(hour=20),
                last_sent=date(2026, 7, 15),
                morning_hour=8,
                evening_hour=19,
            )
        )

    def test_evening_nudge_is_once_per_day_after_threshold(self) -> None:
        now = datetime(2026, 7, 16, 19, 0, tzinfo=self.tz)
        self.assertTrue(
            _evening_due(
                now,
                last_sent=date(2026, 7, 15),
                evening_hour=19,
            )
        )
        self.assertFalse(
            _evening_due(
                now,
                last_sent=date(2026, 7, 16),
                evening_hour=19,
            )
        )
        self.assertFalse(
            _evening_due(
                now.replace(hour=18),
                last_sent=date(2026, 7, 15),
                evening_hour=19,
            )
        )

    def test_action_word_pluralization(self) -> None:
        self.assertEqual(_action_word(1), "действие")
        self.assertEqual(_action_word(2), "действия")
        self.assertEqual(_action_word(5), "действий")
        self.assertEqual(_action_word(11), "действий")
        self.assertEqual(_action_word(21), "действие")


class ProductModeInstantCaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_safe_task_executes_without_confirmation_preview(self) -> None:
        message = SimpleNamespace(chat=SimpleNamespace(id=42), message_id=100)
        original_preview = AsyncMock(return_value=999)
        create_pending = AsyncMock(return_value=7)
        remember = AsyncMock()
        execute = AsyncMock(return_value="✅ Задача создана")

        with (
            patch.object(product_mode, "_original_create_pending_preview", original_preview),
            patch.object(product_mode, "create_pending_action", create_pending),
            patch.object(product_mode, "remember_recent_action", remember),
            patch.object(product_mode, "execute_pending_action", execute),
            patch.dict("bot.services.product_mode.os.environ", {"ASSISTANT_INSTANT_CAPTURE": "1"}),
        ):
            result = await product_mode._instant_create_pending_preview(
                message,
                db_pool=_Pool(),
                deps=SimpleNamespace(),
                kind="task",
                payload={"title": "Подготовить расчёт", "project_id": 5},
                fingerprint="task-fingerprint",
                summary="Подготовить расчёт",
                source="text.batch0",
            )

        self.assertEqual(result, 7)
        original_preview.assert_not_awaited()
        create_pending.assert_awaited_once()
        remember.assert_awaited_once()
        execute.assert_awaited_once()

    async def test_event_keeps_confirmation_preview(self) -> None:
        message = SimpleNamespace(chat=SimpleNamespace(id=42), message_id=101)
        original_preview = AsyncMock(return_value=88)

        with (
            patch.object(product_mode, "_original_create_pending_preview", original_preview),
            patch.dict("bot.services.product_mode.os.environ", {"ASSISTANT_INSTANT_CAPTURE": "1"}),
        ):
            result = await product_mode._instant_create_pending_preview(
                message,
                db_pool=_Pool(),
                deps=SimpleNamespace(),
                kind="event",
                payload={"title": "Встреча"},
                fingerprint="event-fingerprint",
                summary="Встреча",
                source="text",
            )

        self.assertEqual(result, 88)
        original_preview.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
