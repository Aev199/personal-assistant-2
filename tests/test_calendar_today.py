import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from bot.adapters.icloud_caldav_adapter import ICloudVisibleEvent
from bot.services import calendar_today


class _Calendar:
    def __init__(self, events_by_url):
        self.events_by_url = events_by_url
        self.calls = []

    async def list_events(self, url, *, start_utc, end_utc):
        self.calls.append((url, start_utc, end_utc))
        return list(self.events_by_url.get(url, []))


class CalendarTodayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        calendar_today._CACHE.clear()

    async def test_empty_configuration_needs_no_calendar_client(self):
        result = await calendar_today.fetch_today_calendar(
            tz=ZoneInfo("Europe/Moscow"),
            calendar_urls=[],
            icloud=None,
        )
        self.assertEqual(result.events, ())
        self.assertFalse(result.unavailable)
        self.assertFalse(result.pending)

    async def test_configured_but_unavailable_calendar_is_explicit(self):
        result = await calendar_today.fetch_today_calendar(
            tz=ZoneInfo("Europe/Moscow"),
            calendar_urls=["cal://work"],
            icloud=None,
        )
        self.assertEqual(result.events, ())
        self.assertTrue(result.unavailable)
        self.assertFalse(result.pending)

    async def test_deduplicates_cross_calendar_copy_and_uses_short_cache(self):
        start = datetime(2026, 9, 22, 9, 0, tzinfo=timezone.utc)
        short = ICloudVisibleEvent(
            calendar_url="cal://work",
            summary="Планёрка",
            dtstart_utc=start,
            dtend_utc=start + timedelta(minutes=30),
            uid="work-copy",
        )
        long = ICloudVisibleEvent(
            calendar_url="cal://bitrix",
            summary="Планёрка",
            dtstart_utc=start,
            dtend_utc=start + timedelta(minutes=45),
            uid="bitrix-copy",
        )
        client = _Calendar(
            {
                "cal://work": [short],
                "cal://bitrix": [long],
            }
        )

        first = await calendar_today.fetch_today_calendar(
            tz=ZoneInfo("Europe/Moscow"),
            calendar_urls=["cal://work", "cal://bitrix"],
            icloud=client,
            cache_ttl_sec=90,
        )
        second = await calendar_today.fetch_today_calendar(
            tz=ZoneInfo("Europe/Moscow"),
            calendar_urls=["cal://work", "cal://bitrix"],
            icloud=client,
            cache_ttl_sec=90,
        )

        self.assertEqual(len(first.events), 1)
        self.assertEqual(first.events[0].dtend_utc, long.dtend_utc)
        self.assertEqual(second, first)
        self.assertEqual(len(client.calls), 2)


if __name__ == "__main__":
    unittest.main()
