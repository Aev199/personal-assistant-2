import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class LegacyTodayCleanupTests(unittest.TestCase):
    def test_legacy_today_keyboard_file_is_removed(self) -> None:
        self.assertFalse((ROOT / "bot" / "keyboards" / "today.py").exists())

    def test_system_handler_has_no_today_pick_callback(self) -> None:
        source = (ROOT / "bot" / "handlers" / "system.py").read_text(encoding="utf-8")
        self.assertNotIn("cb_today_pick", source)
        self.assertNotIn("nav:today:pick", source)


if __name__ == "__main__":
    unittest.main()
