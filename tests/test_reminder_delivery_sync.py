import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.services.reminders import mark_telegram_reminder_snoozed, send_reminder
from bot.services.tick import _ack_sent


class ReminderDeliverySyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_reminder_returns_telegram_message_id(self):
        bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=321))
        )

        message_id = await send_reminder(
            bot=bot,
            chat_id=42,
            reminder_id=7,
            text="Позвонить",
            send_timeout_sec=1,
            action_token="abc",
        )

        self.assertEqual(message_id, 321)
        bot.send_message.assert_awaited_once()

    async def test_ack_sent_persists_telegram_message_id(self):
        conn = AsyncMock()

        await _ack_sent(
            conn,
            reminder_id=7,
            claim_token="00000000-0000-0000-0000-000000000001",
            repeat="none",
            remind_at=datetime.now(timezone.utc).replace(tzinfo=None),
            tz_name="Europe/Moscow",
            telegram_message_id=321,
        )

        sql, reminder_id, claim_token, message_id = conn.execute.await_args.args
        self.assertIn("telegram_message_id=$3", sql)
        self.assertEqual(reminder_id, 7)
        self.assertEqual(message_id, 321)

    async def test_native_snooze_marks_telegram_alert_inactive(self):
        bot = SimpleNamespace(
            edit_message_text=AsyncMock(),
            edit_message_reply_markup=AsyncMock(),
        )

        await mark_telegram_reminder_snoozed(
            bot=bot,
            chat_id=42,
            message_id=321,
            text="Позвонить",
            label="15 минут",
        )

        bot.edit_message_text.assert_awaited_once()
        kwargs = bot.edit_message_text.await_args.kwargs
        self.assertEqual(kwargs["chat_id"], 42)
        self.assertEqual(kwargs["message_id"], 321)
        self.assertIn("🔕 Отложено на 15 минут", kwargs["text"])
        self.assertIsNone(kwargs["reply_markup"])
        bot.edit_message_reply_markup.assert_not_awaited()

    async def test_telegram_cleanup_falls_back_to_removing_buttons(self):
        bot = SimpleNamespace(
            edit_message_text=AsyncMock(side_effect=RuntimeError("cannot edit")),
            edit_message_reply_markup=AsyncMock(),
        )

        await mark_telegram_reminder_snoozed(
            bot=bot,
            chat_id=42,
            message_id=321,
            text="Позвонить",
        )

        bot.edit_message_reply_markup.assert_awaited_once_with(
            chat_id=42,
            message_id=321,
            reply_markup=None,
        )


if __name__ == "__main__":
    unittest.main()
