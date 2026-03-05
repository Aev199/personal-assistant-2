import unittest

from bot.ui.render import _pick_edit_targets


class UiRenderTargetingTests(unittest.TestCase):
    def test_prefers_stored_ui_over_editable_stale_fallback(self) -> None:
        self.assertEqual(_pick_edit_targets(42, None, True, 99, False), [42])

    def test_uses_fallback_when_stored_ui_is_missing(self) -> None:
        self.assertEqual(_pick_edit_targets(None, None, True, 99, False), [99])

    def test_prefers_explicit_preferred_message_before_stored_ui(self) -> None:
        self.assertEqual(_pick_edit_targets(42, 99, True, 77, False), [99, 42])

    def test_skips_duplicate_when_preferred_equals_stored_ui(self) -> None:
        self.assertEqual(_pick_edit_targets(42, 42, True, 77, False), [42])

    def test_force_new_skips_edit_targets(self) -> None:
        self.assertEqual(_pick_edit_targets(42, 99, True, 77, True), [])


if __name__ == "__main__":
    unittest.main()
