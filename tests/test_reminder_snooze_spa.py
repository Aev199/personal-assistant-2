import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.handlers.reminders import cb_rem_snooze


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Conn:
    async def fetchrow(self, query, *_args):
        if "SELECT text, chat_id FROM reminders" in query:
            return {"text": "Напомнить", "chat_id": 10}
        raise AssertionError(f"Unexpected query: {query}")

    async def execute(self, _query, *_args):
        return "OK"

    def transaction(self):
        return _Tx()


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


class ReminderSnoozeSpaTests(unittest.IsolatedAsyncioTestCase):
    async def test_snooze_from_alert_deletes_popup_and_rerenders_current_screen(self) -> None:
        callback = SimpleNamespace(
            data="rem:snooze:15:5:tok123",
            message=SimpleNamespace(chat=SimpleNamespace(id=10), message_id=777),
            answer=AsyncMock(),
        )
        deps = SimpleNamespace(tz_name="Europe/Moscow", db_reminders_remind_at_timestamptz=False)

        with (
            patch("bot.handlers.reminders.record_action_journal", AsyncMock(return_value=1)),
            patch("bot.handlers.reminders.ui_get_state", AsyncMock(return_value={"ui_payload": {}})),
            patch("bot.handlers.reminders.ui_set_state", AsyncMock()),
            patch("bot.handlers.nav._rerender_current_screen", AsyncMock(return_value=900)) as rerender,
            patch("bot.handlers.reminders.try_delete_user_message", AsyncMock()) as delete_msg,
        ):
            await cb_rem_snooze(callback, _Pool(_Conn()), deps)

        rerender.assert_awaited_once()
        delete_msg.assert_awaited_once()

    async def test_snooze_from_reminders_screen_keeps_spa_message(self) -> None:
        callback = SimpleNamespace(
            data="rem:snooze:15:5:2",
            message=SimpleNamespace(chat=SimpleNamespace(id=10), message_id=777),
            answer=AsyncMock(),
        )
        deps = SimpleNamespace(tz_name="Europe/Moscow", db_reminders_remind_at_timestamptz=False)

        with (
            patch("bot.handlers.reminders.record_action_journal", AsyncMock(return_value=1)),
            patch("bot.handlers.reminders.ui_get_state", AsyncMock(return_value={"ui_payload": {}})),
            patch("bot.handlers.reminders.ui_set_state", AsyncMock()),
            patch("bot.handlers.nav._rerender_current_screen", AsyncMock(return_value=777)) as rerender,
            patch("bot.handlers.reminders.try_delete_user_message", AsyncMock()) as delete_msg,
        ):
            await cb_rem_snooze(callback, _Pool(_Conn()), deps)

        rerender.assert_awaited_once()
        delete_msg.assert_not_awaited()

    async def test_snooze_tomorrow_uses_fixed_9am_label(self) -> None:
        callback = SimpleNamespace(
            data="rem:snooze:tom:5:2",
            message=SimpleNamespace(chat=SimpleNamespace(id=10), message_id=777),
            answer=AsyncMock(),
        )
        deps = SimpleNamespace(tz_name="Europe/Moscow", db_reminders_remind_at_timestamptz=False)

        with (
            patch("bot.handlers.reminders.record_action_journal", AsyncMock(return_value=1)),
            patch("bot.handlers.reminders.ui_get_state", AsyncMock(return_value={"ui_payload": {}})),
            patch("bot.handlers.reminders.ui_set_state", AsyncMock()),
            patch("bot.handlers.nav._rerender_current_screen", AsyncMock(return_value=777)),
            patch("bot.handlers.reminders.try_delete_user_message", AsyncMock()),
        ):
            await cb_rem_snooze(callback, _Pool(_Conn()), deps)

        callback.answer.assert_awaited_with("⏸ Отложено на завтра 09:00")

    async def test_snooze_at18_uses_calendar_time_label(self) -> None:
        callback = SimpleNamespace(
            data="rem:snooze:at18:5:2",
            message=SimpleNamespace(chat=SimpleNamespace(id=10), message_id=777),
            answer=AsyncMock(),
        )
        deps = SimpleNamespace(tz_name="Europe/Moscow", db_reminders_remind_at_timestamptz=False)

        with (
            patch("bot.handlers.reminders.record_action_journal", AsyncMock(return_value=1)),
            patch("bot.handlers.reminders.ui_get_state", AsyncMock(return_value={"ui_payload": {}})),
            patch("bot.handlers.reminders.ui_set_state", AsyncMock()),
            patch("bot.handlers.nav._rerender_current_screen", AsyncMock(return_value=777)),
            patch("bot.handlers.reminders.try_delete_user_message", AsyncMock()),
        ):
            await cb_rem_snooze(callback, _Pool(_Conn()), deps)

        call_args = callback.answer.await_args_list
        self.assertTrue(call_args)
        self.assertIn("18:00", str(call_args[-1].args[0]))


if __name__ == "__main__":
    unittest.main()
