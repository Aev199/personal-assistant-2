import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.ui.screens import ui_render_today


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


class _Conn:
    async def fetchval(self, query, *_args):
        if "FROM tasks t" in query and "COUNT(*)" in query:
            return 1
        raise AssertionError(f"Unexpected fetchval query: {query}")

    async def fetch(self, query, *_args):
        if "FROM tasks t" in query:
            return [
                {
                    "id": 42,
                    "title": "РџРѕР·РІРѕРЅРёС‚СЊ РєР»РёРµРЅС‚Сѓ",
                    "project": "CRM",
                    "assignee": "вЂ”",
                    "deadline": None,
                }
            ]
        if "FROM reminders" in query:
            return []
        raise AssertionError(f"Unexpected fetch query: {query}")


class TodayScreenContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_today_screen_is_action_ready_without_pick_screen(self) -> None:
        message = SimpleNamespace(chat=SimpleNamespace(id=101), bot=SimpleNamespace())
        conn = _Conn()
        pool = _Pool(conn)

        with (
            patch("bot.ui.screens._pop_screen_toast", AsyncMock(return_value=None)),
            patch("bot.ui.screens.ui_render", AsyncMock(return_value=1)) as render,
        ):
            await ui_render_today(message, pool, tz_name="Europe/Moscow", page=0)

        kwargs = render.await_args.kwargs
        self.assertEqual(kwargs["screen"], "today")
        callbacks = [btn.callback_data for row in kwargs["reply_markup"].inline_keyboard for btn in row]
        self.assertIn("task:42", callbacks)
        self.assertNotIn("nav:today:pick:0", callbacks)

    async def test_today_screen_shows_calendar_events_summary(self) -> None:
        message = SimpleNamespace(chat=SimpleNamespace(id=102), bot=SimpleNamespace())
        pool = _Pool(_Conn())
        icloud = SimpleNamespace(
            list_events=AsyncMock(
                side_effect=[
                    [
                        SimpleNamespace(
                            calendar_url="work://calendar",
                            summary="Р”РµР№Р»Рё",
                            dtstart_utc=datetime(2026, 3, 18, 7, 0, tzinfo=timezone.utc),
                            dtend_utc=datetime(2026, 3, 18, 7, 30, tzinfo=timezone.utc),
                            uid="w1",
                        ),
                        SimpleNamespace(
                            calendar_url="other://calendar",
                            summary="РЎРѕР·РІРѕРЅ",
                            dtstart_utc=datetime(2026, 3, 18, 18, 0, tzinfo=timezone.utc),
                            dtend_utc=datetime(2026, 3, 18, 19, 0, tzinfo=timezone.utc),
                            uid="o1",
                        ),
                    ],
                    [
                        SimpleNamespace(
                            calendar_url="personal://calendar",
                            summary="РЎРїРѕСЂС‚Р·Р°Р»",
                            dtstart_utc=datetime(2026, 3, 18, 16, 0, tzinfo=timezone.utc),
                            dtend_utc=datetime(2026, 3, 18, 17, 0, tzinfo=timezone.utc),
                            uid="p1",
                        ),
                        SimpleNamespace(
                            calendar_url="personal://calendar",
                            summary="РЈР¶РёРЅ",
                            dtstart_utc=datetime(2026, 3, 18, 20, 0, tzinfo=timezone.utc),
                            dtend_utc=datetime(2026, 3, 18, 21, 0, tzinfo=timezone.utc),
                            uid="p2",
                        ),
                    ],
                ]
            )
        )

        with (
            patch("bot.ui.screens._pop_screen_toast", AsyncMock(return_value=None)),
            patch("bot.ui.screens.os.getenv", side_effect=lambda key: {"ICLOUD_CALENDAR_URL_WORK": "work://calendar", "ICLOUD_CALENDAR_URL_PERSONAL": "personal://calendar"}.get(key, "")),
            patch("bot.ui.screens.ui_render", AsyncMock(return_value=1)) as render,
        ):
            await ui_render_today(message, pool, tz_name="Europe/Moscow", page=0, icloud=icloud)

        text = render.await_args.kwargs["text"]
        self.assertIn("РЎРѕР±С‹С‚РёР№: 4", text)
        self.assertIn("<b>рџ“… РЎРѕР±С‹С‚РёСЏ</b>", text)
        self.assertIn("рџ’ј <b>10:00вЂ“10:30</b> вЂў Р”РµР№Р»Рё", text)
        self.assertIn("рџЏЎ <b>19:00вЂ“20:00</b> вЂў РЎРїРѕСЂС‚Р·Р°Р»", text)
        self.assertIn("рџ“… <b>21:00вЂ“22:00</b> вЂў РЎРѕР·РІРѕРЅ", text)
        self.assertIn("вЂ¦ РµС‰С‘ 1", text)

    async def test_today_screen_shows_calendar_unavailable_fallback(self) -> None:
        message = SimpleNamespace(chat=SimpleNamespace(id=103), bot=SimpleNamespace())
        pool = _Pool(_Conn())
        icloud = SimpleNamespace(list_events=AsyncMock(side_effect=RuntimeError("calendar offline")))

        with (
            patch("bot.ui.screens._pop_screen_toast", AsyncMock(return_value=None)),
            patch("bot.ui.screens.os.getenv", side_effect=lambda key: {"ICLOUD_CALENDAR_URL_WORK": "work://calendar"}.get(key, "")),
            patch("bot.ui.screens.ui_render", AsyncMock(return_value=1)) as render,
        ):
            await ui_render_today(message, pool, tz_name="Europe/Moscow", page=0, icloud=icloud)

        text = render.await_args.kwargs["text"]
        self.assertIn("РЎРѕР±С‹С‚РёР№: 0", text)
        self.assertIn("РЎРѕР±С‹С‚РёСЏ РІСЂРµРјРµРЅРЅРѕ РЅРµРґРѕСЃС‚СѓРїРЅС‹", text)


if __name__ == "__main__":
    unittest.main()
