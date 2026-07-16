import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

from bot.services.product_mode import (
    SAFE_INSTANT_KINDS,
    _action_word,
    _evening_due,
    _morning_due,
)


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


if __name__ == "__main__":
    unittest.main()
