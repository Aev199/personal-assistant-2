import unittest

from bot.keyboards.common import main_menu_kb


class MainMenuKeyboardTests(unittest.TestCase):
    def test_main_menu_is_persistent(self) -> None:
        kb = main_menu_kb()
        self.assertTrue(kb.is_persistent)
        self.assertTrue(kb.resize_keyboard)
        self.assertFalse(kb.one_time_keyboard)


if __name__ == "__main__":
    unittest.main()
