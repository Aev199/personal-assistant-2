import unittest

from bot.handlers.nav import _parse_nav_all_callback


class NavAllParserTests(unittest.TestCase):
    def test_plain_nav_all(self) -> None:
        self.assertEqual(_parse_nav_all_callback("nav:all"), ("all", 0))

    def test_legacy_page_only(self) -> None:
        self.assertEqual(_parse_nav_all_callback("nav:all:2"), ("all", 2))

    def test_filter_only(self) -> None:
        self.assertEqual(_parse_nav_all_callback("nav:all:today"), ("today", 0))

    def test_filter_with_page(self) -> None:
        self.assertEqual(_parse_nav_all_callback("nav:all:overdue:4"), ("overdue", 4))

    def test_unknown_filter_falls_back_to_all(self) -> None:
        self.assertEqual(_parse_nav_all_callback("nav:all:weird"), ("all", 0))

    def test_unknown_filter_keeps_valid_page(self) -> None:
        self.assertEqual(_parse_nav_all_callback("nav:all:weird:7"), ("all", 7))

    def test_invalid_page_falls_back_to_zero(self) -> None:
        self.assertEqual(_parse_nav_all_callback("nav:all:nodate:bad"), ("nodate", 0))

    def test_none_data_is_safe(self) -> None:
        self.assertEqual(_parse_nav_all_callback(None), ("all", 0))


if __name__ == "__main__":
    unittest.main()
