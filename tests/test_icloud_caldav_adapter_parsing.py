import unittest
from datetime import datetime, timezone

from bot.adapters.icloud_caldav_adapter import _parse_caldav_multistatus


def _xml_with_calendar_data(ics: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
        "<d:response><d:propstat><d:prop>"
        f"<c:calendar-data>{ics}</c:calendar-data>"
        "</d:prop></d:propstat></d:response>"
        "</d:multistatus>"
    )


class ICloudCalDavParsingTests(unittest.TestCase):
    def test_dedup_prefers_richer_duration_for_same_uid_and_start(self) -> None:
        ics = (
            "BEGIN:VCALENDAR\n"
            "BEGIN:VEVENT\n"
            "UID:dup-1\n"
            "SUMMARY:Daily\n"
            "DTSTART:20260402T100000Z\n"
            "DTEND:20260402T100000Z\n"
            "END:VEVENT\n"
            "BEGIN:VEVENT\n"
            "UID:dup-1\n"
            "SUMMARY:Daily\n"
            "DTSTART:20260402T100000Z\n"
            "DURATION:PT1H\n"
            "END:VEVENT\n"
            "END:VCALENDAR"
        )
        events = _parse_caldav_multistatus("work://calendar/", _xml_with_calendar_data(ics))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].dtstart_utc, datetime(2026, 4, 2, 10, 0, tzinfo=timezone.utc))
        self.assertEqual(events[0].dtend_utc, datetime(2026, 4, 2, 11, 0, tzinfo=timezone.utc))

    def test_parses_quoted_tzid(self) -> None:
        ics = (
            "BEGIN:VCALENDAR\n"
            "BEGIN:VEVENT\n"
            "UID:tzid-1\n"
            "SUMMARY:TZ\n"
            'DTSTART;TZID="UTC":20260402T120000\n'
            'DTEND;TZID="UTC":20260402T123000\n'
            "END:VEVENT\n"
            "END:VCALENDAR"
        )
        events = _parse_caldav_multistatus("work://calendar/", _xml_with_calendar_data(ics))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].dtstart_utc, datetime(2026, 4, 2, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(events[0].dtend_utc, datetime(2026, 4, 2, 12, 30, tzinfo=timezone.utc))


if __name__ == "__main__":
    unittest.main()
