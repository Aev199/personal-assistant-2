import os
import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from bot.services.product_mode_spa import _habit_policy


class HabitPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tz = ZoneInfo("Europe/Riga")
        self.actionable = {"today": 1, "overdue": 0, "reminders": 0, "inbox": 1}

    def test_weekend_brief_is_off_by_default(self):
        saturday = datetime(2026, 8, 1, 8, 0, tzinfo=self.tz)
        with patch.dict(os.environ, {}, clear=True):
            policy = _habit_policy(
                now_local=saturday,
                snapshot=self.actionable,
                onboarding_completed=True,
                active_internal=10,
            )
        self.assertFalse(policy["morning_allowed"])
        self.assertEqual(policy["reason"], "weekend_suppressed")

    def test_empty_weekday_brief_is_suppressed(self):
        monday = datetime(2026, 8, 3, 8, 0, tzinfo=self.tz)
        with patch.dict(os.environ, {}, clear=True):
            policy = _habit_policy(
                now_local=monday,
                snapshot={"today": 0, "overdue": 0, "reminders": 0, "inbox": 12},
                onboarding_completed=True,
                active_internal=12,
            )
        self.assertFalse(policy["morning_allowed"])
        self.assertEqual(policy["reason"], "no_actionable_morning_content")

    def test_notifications_wait_for_initial_population(self):
        monday = datetime(2026, 8, 3, 8, 0, tzinfo=self.tz)
        with patch.dict(os.environ, {}, clear=True):
            policy = _habit_policy(
                now_local=monday,
                snapshot=self.actionable,
                onboarding_completed=False,
                active_internal=1,
            )
        self.assertFalse(policy["ready"])
        self.assertFalse(policy["morning_allowed"])
        self.assertEqual(policy["reason"], "awaiting_onboarding")

    def test_actionable_weekday_brief_is_allowed_after_setup(self):
        monday = datetime(2026, 8, 3, 8, 0, tzinfo=self.tz)
        with patch.dict(os.environ, {}, clear=True):
            policy = _habit_policy(
                now_local=monday,
                snapshot=self.actionable,
                onboarding_completed=True,
                active_internal=1,
            )
        self.assertTrue(policy["morning_allowed"])
        self.assertFalse(policy["evening_allowed"])

    def test_evening_nudge_is_explicit_opt_in_and_thresholded(self):
        monday = datetime(2026, 8, 3, 19, 0, tzinfo=self.tz)
        env = {
            "ASSISTANT_EVENING_NUDGE": "1",
            "ASSISTANT_EVENING_INBOX_THRESHOLD": "3",
        }
        with patch.dict(os.environ, env, clear=True):
            low = _habit_policy(
                now_local=monday,
                snapshot={**self.actionable, "inbox": 2},
                onboarding_completed=True,
                active_internal=10,
            )
            enough = _habit_policy(
                now_local=monday,
                snapshot={**self.actionable, "inbox": 3},
                onboarding_completed=True,
                active_internal=10,
            )
        self.assertFalse(low["evening_allowed"])
        self.assertTrue(enough["evening_allowed"])


if __name__ == "__main__":
    unittest.main()
