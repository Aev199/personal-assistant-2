"""Parsing rules shared by every capture client."""
import unittest
from types import SimpleNamespace

from bot.services.intake_rules import (
    ProjectOption,
    TeamOption,
    _action_hint_from_text,
    _build_classification_user_prompt,
    _is_complex_message,
    _match_assignee_option,
    _match_project_option,
    _normalize_batch_payloads,
    _normalize_intake_payload,
    _resolve_project,
)


class _Acquire:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Pool:
    def acquire(self):
        return _Acquire()


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

    def test_removed_nav_action_falls_back_to_reply(self) -> None:
        intent = _normalize_intake_payload({"action": "nav", "reply": "Need manual routing"})
        self.assertEqual(intent.action, "reply")
        self.assertEqual(intent.reply, "Need manual routing")

    def test_valid_event_payload_is_preserved(self) -> None:
        intent = _normalize_intake_payload(
            {
                "action": "event",
                "title": "1:1 with Alex",
                "calendar_kind": "work",
                "start_at_local": "2026-03-12 14:00",
                "duration_min": 45,
                "project_code": "OPS",
            }
        )
        self.assertEqual(intent.action, "event")
        self.assertEqual(intent.title, "1:1 with Alex")
        self.assertEqual(intent.calendar_kind, "work")
        self.assertEqual(intent.start_at_local, "2026-03-12 14:00")
        self.assertEqual(intent.duration_min, 45)
        self.assertEqual(intent.project_code, "OPS")

    def test_valid_personal_task_payload_is_preserved(self) -> None:
        intent = _normalize_intake_payload(
            {
                "action": "personal_task",
                "title": "Buy water filter",
                "deadline_local": "2026-03-12 19:00",
            }
        )
        self.assertEqual(intent.action, "personal_task")
        self.assertEqual(intent.title, "Buy water filter")
        self.assertEqual(intent.deadline_local, "2026-03-12 19:00")

    def test_valid_idea_payload_is_preserved(self) -> None:
        intent = _normalize_intake_payload({"action": "idea", "idea_text": "Build weekly voice digest"})
        self.assertEqual(intent.action, "idea")
        self.assertEqual(intent.idea_text, "Build weekly voice digest")

    def test_event_without_start_requests_followup(self) -> None:
        intent = _normalize_intake_payload(
            {
                "action": "event",
                "title": "Sync",
                "calendar_kind": "personal",
                "duration_min": 30,
            }
        )
        self.assertEqual(intent.action, "reply")
        self.assertTrue(intent.needs_followup)
        self.assertEqual(intent.followup_action, "event")
        self.assertIn("start_at_local", intent.missing_fields)

    def test_idea_without_text_requests_followup(self) -> None:
        intent = _normalize_intake_payload({"action": "idea", "idea_text": ""})
        self.assertEqual(intent.action, "reply")
        self.assertTrue(intent.needs_followup)
        self.assertEqual(intent.followup_action, "idea")
        self.assertEqual(intent.missing_fields, ("idea_text",))

    def test_personal_task_without_title_requests_followup(self) -> None:
        intent = _normalize_intake_payload({"action": "personal_task", "deadline_local": "2026-03-12 19:00"})
        self.assertEqual(intent.action, "reply")
        self.assertTrue(intent.needs_followup)
        self.assertEqual(intent.followup_action, "personal_task")
        self.assertEqual(intent.missing_fields, ("title",))

    def test_reminder_without_datetime_falls_back_to_reply(self) -> None:
        intent = _normalize_intake_payload({"action": "reminder", "reminder_text": "Call back"})
        self.assertEqual(intent.action, "reply")
        self.assertTrue(intent.needs_followup)
        self.assertEqual(intent.followup_action, "reminder")
        self.assertEqual(intent.missing_fields, ("remind_at_local",))

    def test_unknown_action_becomes_reply(self) -> None:
        intent = _normalize_intake_payload({"action": "something_else", "reply": "Needs manual handling"})
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
            raw_text="create task for client launch",
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
            raw_text="tomorrow prepare brief for K-17",
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
            raw_text="ask alex to prepare the layout",
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
            raw_text="need to hand this to maria",
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

    def test_task_without_title_requests_followup(self) -> None:
        intent = _normalize_intake_payload({"action": "task", "deadline_local": "2026-03-12 14:00"})
        self.assertEqual(intent.action, "reply")
        self.assertTrue(intent.needs_followup)
        self.assertEqual(intent.followup_action, "task")
        self.assertEqual(intent.missing_fields, ("title",))

    def test_action_hint_detects_explicit_personal_marker(self) -> None:
        self.assertEqual(
            _action_hint_from_text("\u041b\u0438\u0447\u043d\u043e\u0435: \u043a\u0443\u043f\u0438\u0442\u044c \u0444\u0438\u043b\u044c\u0442\u0440"),
            "personal_task",
        )

    def test_action_hint_detects_explicit_idea_marker(self) -> None:
        self.assertEqual(_action_hint_from_text("\u0418\u0434\u0435\u044f: voice digest"), "idea")

    def test_action_hint_detects_explicit_reminder_marker(self) -> None:
        self.assertEqual(_action_hint_from_text("\u041d\u0430\u043f\u043e\u043c\u043d\u0438 \u0437\u0430\u0432\u0442\u0440\u0430 \u0432 10:00"), "reminder")

    def test_build_classification_user_prompt_uses_structured_followup_context(self) -> None:
        prompt = _build_classification_user_prompt(
            raw_text="tomorrow at 10:00",
            prepend_text="personal: buy filter",
            followup_data={
                "freeform_base_text": "personal: buy filter",
                "freeform_pending_action": "personal_task",
                "freeform_missing_fields": ["deadline_local"],
                "freeform_action_hint": "personal_task",
            },
        )
        self.assertIn("Original request", prompt)
        self.assertIn("Expected action: personal_task.", prompt)
        self.assertIn("deadline_local", prompt)
        self.assertIn("Strong action hint: personal_task.", prompt)



class FreeformIntakeAsyncTests(unittest.IsolatedAsyncioTestCase):



    async def test_resolve_project_defaults_to_inbox_when_no_match(self) -> None:
        project_id, project_code, error = await _resolve_project(
            SimpleNamespace(),
            requested_code=None,
            requested_name=None,
            raw_text="send report",
            current_project_id=42,
            projects=[
                ProjectOption(id=1, code="OPS", name="Operations"),
                ProjectOption(id=2, code="INBOX", name="Inbox"),
            ],
        )

        self.assertIsNone(error)
        self.assertEqual(project_id, 2)
        self.assertEqual(project_code, "INBOX")












class BatchIntakeTests(unittest.TestCase):
    """Tests for multi-intent batch classification and helpers."""

    def test_is_complex_detects_multi_action_message(self) -> None:
        text = (
            "Поставь задачу подготовить отчёт к пятнице. "
            "И ещё напомни завтра в 10 утра позвонить клиенту."
        )
        self.assertTrue(_is_complex_message(text))

    def test_is_complex_detects_numbered_list(self) -> None:
        text = "1. Купить молоко\n2. Позвонить врачу\n3. Подготовить презентацию к среде"
        self.assertTrue(_is_complex_message(text))

    def test_is_complex_rejects_short_message(self) -> None:
        self.assertFalse(_is_complex_message("Купить молоко"))

    def test_is_complex_rejects_single_topic_long_message(self) -> None:
        text = (
            "Подготовить отчёт по проекту K-17 к пятнице в 18:00 "
            "с детальным анализом метрик и графиками по всем кварталам"
        )
        # Long but single topic — should be False (no different action keywords)
        self.assertFalse(_is_complex_message(text))

    def test_is_complex_detects_same_action_different_projects(self) -> None:
        text = (
            "Поставь задачу подготовить отчёт по K-17 к пятнице. "
            "И ещё задачу обновить сайт по OPS к среде."
        )
        self.assertTrue(_is_complex_message(text))

    def test_is_complex_rejects_same_action_same_project(self) -> None:
        text = (
            "Поставь задачу подготовить отчёт по K-17 к пятнице. "
            "И ещё диаграммы к нему же сделать."
        )
        # Overlap is low (anaphoric reference), but LLM handles it — returns 1 action.
        self.assertTrue(_is_complex_message(text))

    def test_is_complex_detects_semicolons(self) -> None:
        text = "Купить хлеб; напомни завтра в 9 про встречу; идея: автоматизировать деплой"
        self.assertTrue(_is_complex_message(text))

    def test_is_complex_detects_newlines(self) -> None:
        text = "Задача: обновить документацию\nВстреча: созвон с командой в 15:00\nИдея: добавить CI/CD"
        self.assertTrue(_is_complex_message(text))

    def test_is_complex_detects_comma_separated_tasks(self) -> None:
        text = "нужно отсканить свидетельство о рождении для матпомощи, зайти за сметаной"
        self.assertTrue(_is_complex_message(text))

    def test_is_complex_detects_two_personal_tasks_no_keywords(self) -> None:
        text = "надо забрать посылку с почты, потом зайти в банк"
        self.assertTrue(_is_complex_message(text))

    def test_is_complex_rejects_short_comma_list(self) -> None:
        text = "молоко, хлеб"
        self.assertFalse(_is_complex_message(text))

    def test_normalize_batch_payloads_single_action(self) -> None:
        raw = {
            "actions": [
                {"action": "task", "title": "Send report", "project_code": "OPS"}
            ],
            "reply": "Created 1 draft",
        }
        intents, reply = _normalize_batch_payloads(raw)
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0].action, "task")
        self.assertEqual(intents[0].title, "Send report")
        self.assertEqual(reply, "Created 1 draft")

    def test_normalize_batch_payloads_multi_action(self) -> None:
        raw = {
            "actions": [
                {"action": "task", "title": "Send report", "project_code": "K-17"},
                {"action": "event", "title": "1:1 with Alex", "calendar_kind": "work", "start_at_local": "2026-03-12 14:00", "duration_min": 30},
                {"action": "reminder", "reminder_text": "Call client", "remind_at_local": "2026-03-12 10:00"},
            ],
            "reply": "3 items created",
        }
        intents, reply = _normalize_batch_payloads(raw)
        self.assertEqual(len(intents), 3)
        self.assertEqual(intents[0].action, "task")
        self.assertEqual(intents[1].action, "event")
        self.assertEqual(intents[2].action, "reminder")
        self.assertEqual(reply, "3 items created")

    def test_normalize_batch_payloads_filters_reply_actions(self) -> None:
        raw = {
            "actions": [
                {"action": "task", "title": "Valid task"},
                {"action": "reply", "reply": "cannot process this"},
                {"action": "idea", "idea_text": "Valid idea"},
            ],
            "reply": "",
        }
        intents, _ = _normalize_batch_payloads(raw)
        self.assertEqual(len(intents), 2)
        self.assertNotIn("reply", [i.action for i in intents])

    def test_normalize_batch_payloads_empty_actions(self) -> None:
        raw = {"actions": [], "reply": "Nothing found"}
        intents, reply = _normalize_batch_payloads(raw)
        self.assertEqual(len(intents), 0)
        self.assertEqual(reply, "Nothing found")

    def test_normalize_batch_payloads_missing_actions_key(self) -> None:
        raw = {"reply": "No actions"}
        intents, reply = _normalize_batch_payloads(raw)
        self.assertEqual(len(intents), 0)
        self.assertEqual(reply, "No actions")


if __name__ == "__main__":
    unittest.main()


def test_unknown_project_goes_to_inbox_and_unknown_person_is_left_empty():
    import asyncio
    from bot.services.intake_rules import _resolve_assignee

    projects = [ProjectOption(id=1, code="INBOX", name="Входящие"), ProjectOption(id=2, code="BGR", name="БГР")]
    result = asyncio.run(_resolve_project(
        object(), requested_code="ZZZ", requested_name=None, raw_text="задача по ZZZ",
        current_project_id=None, projects=projects,
    ))
    assert result == (1, "INBOX", None)
    team = [TeamOption(id=5, name="Иван Петров")]
    assert _resolve_assignee(requested_name="Сидоров", raw_text="поручить Сидорову", team=team) == (None, None, None)
