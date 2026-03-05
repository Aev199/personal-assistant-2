import unittest

from bot.ui.render import _pick_edit_targets


class UiRenderTargetingTests(unittest.TestCase):
    def test_prefers_editable_fallback_before_old_ui(self) -> None:
        self.assertEqual(_pick_edit_targets(42, True, 99, False), [99, 42])

    def test_skips_duplicate_old_ui_when_same_as_fallback(self) -> None:
        self.assertEqual(_pick_edit_targets(42, True, 42, False), [42])

    def test_uses_old_ui_when_fallback_is_not_editable(self) -> None:
        self.assertEqual(_pick_edit_targets(42, False, 99, False), [42])

    def test_force_new_skips_edit_targets(self) -> None:
        self.assertEqual(_pick_edit_targets(42, True, 99, True), [])


if __name__ == "__main__":
    unittest.main()
