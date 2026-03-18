import unittest

from bot.keyboards.common import main_menu_kb


class MainMenuKeyboardTests(unittest.TestCase):
    def test_main_menu_is_persistent(self) -> None:
        kb = main_menu_kb()
        self.assertTrue(kb.is_persistent)
        self.assertTrue(kb.resize_keyboard)
        self.assertFalse(kb.one_time_keyboard)
        self.assertEqual(kb.keyboard[0][0].text, "📅 Сегодня")
        self.assertEqual(kb.keyboard[0][1].text, "📋 Все задачи")
        self.assertEqual(kb.keyboard[1][0].text, "📁 Проекты")
        self.assertEqual(kb.keyboard[1][1].text, "🔔 Напоминания")
        self.assertEqual(kb.keyboard[2][0].text, "➕ Добавить")
        self.assertEqual(kb.keyboard[2][1].text, "👥 Команда")


if __name__ == "__main__":
    unittest.main()
