import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from bot.utils.datetime import quick_parse_datetime_ru


MSK = ZoneInfo("Europe/Moscow")


class QuickParseDatetimeTests(unittest.TestCase):
    def test_explicit_date_and_time_is_parsed(self) -> None:
        text = r"\u0434\u0435\u0434\u043b\u0430\u0439\u043d 12.03 14:00".encode("ascii").decode("unicode_escape")
        dt = quick_parse_datetime_ru(text, "Europe/Moscow")
        self.assertIsNotNone(dt)
        assert dt is not None
        self.assertEqual((dt.month, dt.day, dt.hour, dt.minute), (3, 12, 14, 0))

    def test_relative_day_and_time_is_parsed(self) -> None:
        text = r"\u043f\u043e\u0437\u0432\u043e\u043d\u0438\u0442\u044c \u0437\u0430\u0432\u0442\u0440\u0430 15:30".encode("ascii").decode("unicode_escape")
        dt = quick_parse_datetime_ru(text, "Europe/Moscow")
        self.assertIsNotNone(dt)
        assert dt is not None
        expected_date = (datetime.now(MSK) + timedelta(days=1)).date()
        self.assertEqual(dt.astimezone(MSK).date(), expected_date)
        self.assertEqual((dt.hour, dt.minute), (15, 30))

    def test_date_only_can_use_default_time_for_task_flows(self) -> None:
        text = r"\u0434\u0435\u0434\u043b\u0430\u0439\u043d 12.03".encode("ascii").decode("unicode_escape")
        dt = quick_parse_datetime_ru(text, "Europe/Moscow", date_only_time=(18, 0))
        self.assertIsNotNone(dt)
        assert dt is not None
        self.assertEqual((dt.month, dt.day, dt.hour, dt.minute), (3, 12, 18, 0))

    def test_relative_day_only_can_use_default_time_for_task_flows(self) -> None:
        text = r"\u0437\u0430\u0432\u0442\u0440\u0430".encode("ascii").decode("unicode_escape")
        dt = quick_parse_datetime_ru(text, "Europe/Moscow", date_only_time=(18, 0))
        self.assertIsNotNone(dt)
        assert dt is not None
        expected_date = (datetime.now(MSK) + timedelta(days=1)).date()
        self.assertEqual(dt.astimezone(MSK).date(), expected_date)
        self.assertEqual((dt.hour, dt.minute), (18, 0))

    def test_date_only_without_default_time_stays_unparsed(self) -> None:
        text = r"\u0432\u0441\u0442\u0440\u0435\u0447\u0430 12.03".encode("ascii").decode("unicode_escape")
        dt = quick_parse_datetime_ru(text, "Europe/Moscow")
        self.assertIsNone(dt)


if __name__ == "__main__":
    unittest.main()
