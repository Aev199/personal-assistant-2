import unittest
from bot.keyboards.common import main_menu_kb

class MainMenuKeyboardTests(unittest.TestCase):
    def test_legacy_callers_remove_keyboard_in_all_modes(self):
        for mode in ("lead", "solo"):
            for online in (True, False):
                self.assertTrue(main_menu_kb(mode, llm_online=online).remove_keyboard)
