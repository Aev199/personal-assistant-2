import unittest

from bot.keyboards.common import main_menu_kb


class MainMenuKeyboardTests(unittest.TestCase):
    def test_main_menu_is_persistent(self) -> None:
        kb = main_menu_kb()
        self.assertTrue(kb.is_persistent)
        self.assertTrue(kb.resize_keyboard)
        self.assertFalse(kb.one_time_keyboard)
        self.assertEqual(kb.keyboard[0][0].text, "🏠 Домой")
        self.assertEqual(kb.keyboard[2][0].text, "🔔 Напоминания")
        self.assertEqual(kb.keyboard[3][1].text, "❓ Помощь")


if __name__ == "__main__":
    unittest.main()
