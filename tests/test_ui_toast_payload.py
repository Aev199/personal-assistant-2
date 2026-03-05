import unittest

from bot.ui.state import _now_ts, ui_payload_take_toast, ui_payload_with_toast


class UiToastPayloadTests(unittest.TestCase):
    def test_ui_payload_with_toast_sets_text_and_expiration(self) -> None:
        payload = ui_payload_with_toast({"page": 2}, "hello", ttl_sec=15)
        self.assertEqual(payload["page"], 2)
        self.assertEqual(payload["toast"]["text"], "hello")
        self.assertGreaterEqual(int(payload["toast"]["exp"]), _now_ts() + 14)

    def test_ui_payload_take_toast_returns_text_and_clears_payload(self) -> None:
        payload = {"foo": "bar", "toast": {"text": "done", "exp": _now_ts() + 30}}
        text, new_payload = ui_payload_take_toast(payload)
        self.assertEqual(text, "done")
        self.assertEqual(new_payload, {"foo": "bar"})

    def test_ui_payload_take_toast_ignores_expired_toast(self) -> None:
        payload = {"foo": "bar", "toast": {"text": "stale", "exp": _now_ts() - 1}}
        text, new_payload = ui_payload_take_toast(payload)
        self.assertIsNone(text)
        self.assertEqual(new_payload, {"foo": "bar"})


if __name__ == "__main__":
    unittest.main()
