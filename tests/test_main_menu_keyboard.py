import unittest

from bot.keyboards.common import main_menu_kb


class MainMenuKeyboardTests(unittest.TestCase):
    def test_main_menu_is_persistent_and_compact(self) -> None:
        kb = main_menu_kb("lead")
        self.assertTrue(kb.is_persistent)
        self.assertTrue(kb.resize_keyboard)
        self.assertFalse(kb.one_time_keyboard)
        self.assertEqual(kb.keyboard[0][0].text, "📅 Сегодня")
        self.assertEqual(kb.keyboard[0][1].text, "➕ Добавить")
        self.assertEqual(kb.keyboard[1][0].text, "📋 Все задачи")
        self.assertEqual(kb.keyboard[1][1].text, "📁 Проекты")
        self.assertEqual(kb.keyboard[2][0].text, "↩️ Отмена")
        self.assertEqual(len(kb.keyboard), 3)

    def test_menu_is_same_in_solo_mode(self) -> None:
        lead = main_menu_kb("lead")
        solo = main_menu_kb("solo")
        self.assertEqual(
            [[button.text for button in row] for row in lead.keyboard],
            [[button.text for button in row] for row in solo.keyboard],
        )

    def test_offline_mode_keeps_add_position(self) -> None:
        kb = main_menu_kb("lead", llm_online=False)
        self.assertEqual(kb.keyboard[0][1].text, "⚠️ ИИ офлайн")


if __name__ == "__main__":
    unittest.main()
