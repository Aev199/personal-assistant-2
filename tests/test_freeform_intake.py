import unittest

from bot.services.freeform_intake import _normalize_intake_payload


class FreeformIntakeTests(unittest.TestCase):
    def test_valid_task_payload_is_preserved(self) -> None:
        intent = _normalize_intake_payload(
            {
                "action": "task",
                "title": "Отправить отчет",
                "deadline_local": "2026-03-12 14:00",
                "project_code": "K-17",
            }
        )
        self.assertEqual(intent.action, "task")
        self.assertEqual(intent.title, "Отправить отчет")
        self.assertEqual(intent.deadline_local, "2026-03-12 14:00")
        self.assertEqual(intent.project_code, "K-17")

    def test_nav_unknown_screen_falls_back_to_reply(self) -> None:
        intent = _normalize_intake_payload(
            {
                "action": "nav",
                "screen": "weird",
                "reply": "Не понял экран",
            }
        )
        self.assertEqual(intent.action, "reply")
        self.assertEqual(intent.reply, "Не понял экран")

    def test_reminder_without_datetime_falls_back_to_reply(self) -> None:
        intent = _normalize_intake_payload(
            {
                "action": "reminder",
                "reminder_text": "Позвонить",
            }
        )
        self.assertEqual(intent.action, "reply")
        self.assertIn("напоминание", intent.reply.lower())

    def test_unknown_action_becomes_reply(self) -> None:
        intent = _normalize_intake_payload(
            {
                "action": "something_else",
                "reply": "Нужна ручная обработка",
            }
        )
        self.assertEqual(intent.action, "reply")
        self.assertEqual(intent.reply, "Нужна ручная обработка")


if __name__ == "__main__":
    unittest.main()
