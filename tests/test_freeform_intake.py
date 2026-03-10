import unittest

from bot.services.freeform_intake import (
    ProjectOption,
    TeamOption,
    _match_assignee_option,
    _match_project_option,
    _normalize_intake_payload,
)


class FreeformIntakeTests(unittest.TestCase):
    def test_valid_task_payload_is_preserved(self) -> None:
        intent = _normalize_intake_payload(
            {
                "action": "task",
                "title": "Send report",
                "deadline_local": "2026-03-12 14:00",
                "project_code": "K-17",
                "project_name": "Launch",
                "assignee_name": "Alex",
            }
        )
        self.assertEqual(intent.action, "task")
        self.assertEqual(intent.title, "Send report")
        self.assertEqual(intent.deadline_local, "2026-03-12 14:00")
        self.assertEqual(intent.project_code, "K-17")
        self.assertEqual(intent.project_name, "Launch")
        self.assertEqual(intent.assignee_name, "Alex")

    def test_nav_unknown_screen_falls_back_to_reply(self) -> None:
        intent = _normalize_intake_payload(
            {
                "action": "nav",
                "screen": "weird",
                "reply": "Need manual routing",
            }
        )
        self.assertEqual(intent.action, "reply")
        self.assertEqual(intent.reply, "Need manual routing")

    def test_reminder_without_datetime_falls_back_to_reply(self) -> None:
        intent = _normalize_intake_payload(
            {
                "action": "reminder",
                "reminder_text": "Call back",
            }
        )
        self.assertEqual(intent.action, "reply")
        self.assertIn("\u043d\u0430\u043f\u043e\u043c\u0438\u043d\u0430\u043d\u0438\u0435", intent.reply.lower())

    def test_unknown_action_becomes_reply(self) -> None:
        intent = _normalize_intake_payload(
            {
                "action": "something_else",
                "reply": "Needs manual handling",
            }
        )
        self.assertEqual(intent.action, "reply")
        self.assertEqual(intent.reply, "Needs manual handling")

    def test_match_project_by_exact_name_hint(self) -> None:
        project = _match_project_option(
            [
                ProjectOption(id=1, code="K-17", name="Client launch"),
                ProjectOption(id=2, code="OPS", name="Operations"),
            ],
            requested_code=None,
            requested_name="Client launch",
            raw_text="\u043f\u043e\u0441\u0442\u0430\u0432\u044c \u0437\u0430\u0434\u0430\u0447\u0443 \u043f\u043e client launch",
        )
        self.assertIsNotNone(project)
        self.assertEqual(project.code, "K-17")

    def test_match_project_by_code_inside_text(self) -> None:
        project = _match_project_option(
            [
                ProjectOption(id=1, code="K-17", name="Client launch"),
                ProjectOption(id=2, code="OPS", name="Operations"),
            ],
            requested_code=None,
            requested_name=None,
            raw_text="\u0437\u0430\u0432\u0442\u0440\u0430 \u043f\u043e\u0434\u0433\u043e\u0442\u043e\u0432\u0438\u0442\u044c \u0431\u0440\u0438\u0444 \u0434\u043b\u044f K-17",
        )
        self.assertIsNotNone(project)
        self.assertEqual(project.code, "K-17")

    def test_match_assignee_by_unique_first_name(self) -> None:
        assignee = _match_assignee_option(
            [
                TeamOption(id=1, name="Alex Ivanov"),
                TeamOption(id=2, name="Maria Petrova"),
            ],
            requested_name=None,
            raw_text="\u043f\u043e\u0441\u0442\u0430\u0432\u044c alex \u0441\u0434\u0435\u043b\u0430\u0442\u044c \u043c\u0430\u043a\u0435\u0442",
        )
        self.assertIsNotNone(assignee)
        self.assertEqual(assignee.name, "Alex Ivanov")

    def test_match_assignee_by_full_name_hint(self) -> None:
        assignee = _match_assignee_option(
            [
                TeamOption(id=1, name="Alex Ivanov"),
                TeamOption(id=2, name="Maria Petrova"),
            ],
            requested_name="Maria Petrova",
            raw_text="\u043d\u0443\u0436\u043d\u043e \u043f\u0435\u0440\u0435\u0434\u0430\u0442\u044c \u043c\u0430\u0440\u0438\u0438",
        )
        self.assertIsNotNone(assignee)
        self.assertEqual(assignee.name, "Maria Petrova")

    def test_match_assignee_by_inflected_first_name(self) -> None:
        assignee = _match_assignee_option(
            [
                TeamOption(id=1, name="\u0421\u0430\u0448\u0430 \u0418\u0432\u0430\u043d\u043e\u0432"),
                TeamOption(id=2, name="\u041c\u0430\u0440\u0438\u044f \u041f\u0435\u0442\u0440\u043e\u0432\u0430"),
            ],
            requested_name=None,
            raw_text="\u043f\u043e\u0441\u0442\u0430\u0432\u044c \u0421\u0430\u0448\u0435 \u043f\u043e\u0434\u0433\u043e\u0442\u043e\u0432\u0438\u0442\u044c \u0441\u043c\u0435\u0442\u0443",
        )
        self.assertIsNotNone(assignee)
        self.assertEqual(assignee.name, "\u0421\u0430\u0448\u0430 \u0418\u0432\u0430\u043d\u043e\u0432")


if __name__ == "__main__":
    unittest.main()
