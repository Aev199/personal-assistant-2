import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.services.reminders import mark_telegram_reminder_snoozed, send_reminder
from bot.services.tick import (
    _ack_failed,
    _ack_sent,
    _claim_is_current,
    _discard_late_telegram_reminder,
)


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
        markup = bot.send_message.await_args.kwargs["reply_markup"]
        self.assertEqual(
            markup.inline_keyboard[0][0].callback_data,
            "rem:close:7:abc",
        )

    async def test_new_claim_invalidates_previous_occurrence_message_id(self):
        from pathlib import Path
        source = (Path(__file__).resolve().parents[1] / "bot" / "services" / "tick.py").read_text(encoding="utf-8")
        start = source.index("async def _claim_due_reminders")
        end = source.index("async def _claim_is_current", start)
        claim_block = source[start:end]

        self.assertIn("telegram_message_id=NULL", claim_block)


    async def test_claim_recheck_detects_native_action_before_telegram_send(self):
        conn = AsyncMock()
        conn.fetchval.return_value = False

        current = await _claim_is_current(
            conn,
            reminder_id=7,
            claim_token="00000000-0000-0000-0000-000000000001",
        )

        self.assertFalse(current)
        sql, reminder_id, claim_token = conn.fetchval.await_args.args
        self.assertIn("status='claimed'", sql)
        self.assertIn("claim_token=$2::uuid", sql)
        self.assertEqual(reminder_id, 7)

    async def test_tick_rechecks_claim_immediately_before_telegram_delivery(self):
        from pathlib import Path
        source = (Path(__file__).resolve().parents[1] / "bot" / "services" / "tick.py").read_text(encoding="utf-8")
        loop_start = source.index("for record in records:")
        send_pos = source.index("telegram_message_id = await send_reminder", loop_start)
        recheck_pos = source.index("await _claim_is_current", loop_start)

        self.assertLess(recheck_pos, send_pos)


    async def test_failed_send_does_not_resurrect_reminder_after_native_action_wins(self):
        conn = AsyncMock()
        conn.execute.return_value = "UPDATE 0"

        status = await _ack_failed(
            conn,
            reminder_id=7,
            claim_token="00000000-0000-0000-0000-000000000001",
            attempt_count=1,
            max_attempts=6,
            error_code="telegram_send_failed",
        )

        self.assertEqual(status, "lost")

    async def test_exhausted_send_does_not_mark_newer_occurrence_failed(self):
        conn = AsyncMock()
        conn.execute.return_value = "UPDATE 0"

        status = await _ack_failed(
            conn,
            reminder_id=7,
            claim_token="00000000-0000-0000-0000-000000000001",
            attempt_count=6,
            max_attempts=6,
            error_code="telegram_send_failed",
        )

        self.assertEqual(status, "lost")


    async def test_ack_sent_persists_telegram_message_id(self):
        conn = AsyncMock()
        conn.execute.return_value = "UPDATE 1"

        accepted = await _ack_sent(
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
        self.assertTrue(accepted)

    async def test_repeating_delivery_keeps_last_message_id_for_stale_callback_guard(self):
        conn = AsyncMock()
        conn.execute.return_value = "UPDATE 1"

        accepted = await _ack_sent(
            conn,
            reminder_id=7,
            claim_token="00000000-0000-0000-0000-000000000001",
            repeat="daily",
            remind_at=datetime.now(timezone.utc).replace(tzinfo=None),
            tz_name="Europe/Moscow",
            telegram_message_id=321,
        )

        self.assertTrue(accepted)
        sql = conn.execute.await_args.args[0]
        self.assertIn("telegram_message_id=$5", sql)

    async def test_ack_sent_reports_lost_claim_instead_of_counting_delivery(self):
        conn = AsyncMock()
        conn.execute.return_value = "UPDATE 0"

        accepted = await _ack_sent(
            conn,
            reminder_id=7,
            claim_token="00000000-0000-0000-0000-000000000001",
            repeat="none",
            remind_at=datetime.now(timezone.utc).replace(tzinfo=None),
            tz_name="Europe/Moscow",
            telegram_message_id=321,
        )

        self.assertFalse(accepted)

    async def test_late_telegram_message_is_deleted_when_native_action_wins_send_race(self):
        bot = SimpleNamespace(
            delete_message=AsyncMock(),
            edit_message_reply_markup=AsyncMock(),
        )

        await _discard_late_telegram_reminder(
            bot=bot,
            chat_id=42,
            message_id=321,
        )

        bot.delete_message.assert_awaited_once_with(chat_id=42, message_id=321)
        bot.edit_message_reply_markup.assert_not_awaited()

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
