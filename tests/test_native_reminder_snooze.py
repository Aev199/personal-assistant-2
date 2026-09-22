import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from bot.services.reminders import snooze_reminder


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Conn:
    def __init__(self):
        self.execute = AsyncMock()
        self.fetchval = AsyncMock(return_value=88)

    async def fetchrow(self, query, *_args):
        if "SELECT text, chat_id FROM reminders" in query:
            return {"text": "Позвонить", "chat_id": 42}
        raise AssertionError(f"Unexpected query: {query}")

    def transaction(self):
        return _Tx()


class NativeReminderSnoozeTests(unittest.IsolatedAsyncioTestCase):
    async def test_snooze_cancels_original_and_creates_one_new_reminder(self):
        conn = _Conn()

        with patch("bot.tz.to_db_utc", side_effect=lambda dt, **_: dt):
            result = await snooze_reminder(
                conn,
                reminder_id=7,
                minutes=15,
                fallback_chat_id=42,
                tz_name="Europe/Moscow",
                store_tz=True,
            )

        self.assertIsNotNone(result)
        new_id, new_time, label = result
        self.assertEqual(new_id, 88)
        self.assertEqual(label, "15 мин")
        self.assertIsInstance(new_time, datetime)
        self.assertEqual(new_time.tzinfo, timezone.utc)

        statements = "\n".join(call.args[0] for call in conn.execute.await_args_list)
        self.assertIn("UPDATE reminders", statements)
        self.assertIn("status='cancelled'", statements)

        insert_sql = conn.fetchval.await_args.args[0]
        self.assertIn("INSERT INTO reminders", insert_sql)
        self.assertIn("RETURNING id", insert_sql)

    async def test_missing_reminder_is_a_noop(self):
        conn = _Conn()

        async def missing(_query, *_args):
            return None

        conn.fetchrow = missing

        result = await snooze_reminder(
            conn,
            reminder_id=999,
            minutes=15,
            fallback_chat_id=42,
            tz_name="Europe/Moscow",
            store_tz=True,
        )

        self.assertIsNone(result)
        conn.execute.assert_not_awaited()
        conn.fetchval.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
