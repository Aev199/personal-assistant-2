import unittest

from bot.adapters.deepseek_adapter import DeepSeekAdapter


class DeepSeekAdapterTests(unittest.TestCase):
    def test_extracts_json_from_markdown_fence(self):
        payload = DeepSeekAdapter._extract_json_object(
            '```json\n{"actions": [{"action": "task", "title": "Проверить отчёт"}], "reply": ""}\n```'
        )
        self.assertEqual(payload["actions"][0]["action"], "task")

    def test_enabled_requires_api_key(self):
        self.assertFalse(DeepSeekAdapter(api_key="").enabled)
        self.assertTrue(DeepSeekAdapter(api_key="secret").enabled)

    def test_extract_text_uses_first_choice(self):
        text = DeepSeekAdapter._extract_text(
            {"choices": [{"message": {"content": '{"action":"idea","reply":""}'}}]}
        )
        self.assertIn('"idea"', text)


if __name__ == "__main__":
    unittest.main()
