import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.services.freeform_intake import (
    ProjectOption,
    TeamOption,
    _action_hint_from_text,
    _build_classification_user_prompt,
    _match_assignee_option,
    _match_project_option,
    _normalize_intake_payload,
    _start_followup,
    _voice_file_meta,
    handle_freeform_text,
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

    def test_voice_file_meta_prefers_voice_payload(self) -> None:
        message = SimpleNamespace(
            voice=SimpleNamespace(
                file_id="voice-file",
                file_unique_id="uniq-1",
                mime_type=None,
                file_size=321,
            ),
            audio=None,
        )
        file_id, filename, mime_type, file_size = _voice_file_meta(message)
        self.assertEqual(file_id, "voice-file")
        self.assertEqual(filename, "voice_uniq-1.ogg")
        self.assertEqual(mime_type, "audio/ogg")
        self.assertEqual(file_size, 321)


class FreeformIntakeAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_followup_persists_structured_context(self) -> None:
        state = AsyncMock()
        message = SimpleNamespace(chat=SimpleNamespace(id=202))

        with patch("bot.services.freeform_intake._rerender_with_toast", AsyncMock(return_value=1)):
            handled = await _start_followup(
                message,
                deps=SimpleNamespace(),
                db_pool=_Pool(),
                state=state,
                prompt="Need deadline",
                base_text="\u043b\u0438\u0447\u043d\u043e\u0435: \u043a\u0443\u043f\u0438\u0442\u044c \u0444\u0438\u043b\u044c\u0442\u0440",
                source="text",
                pending_action="personal_task",
                missing_fields=("deadline_local",),
            )

        self.assertTrue(handled)
        self.assertEqual(state.update_data.await_count, 1)
        kwargs = state.update_data.await_args.kwargs
        self.assertEqual(kwargs["freeform_pending_action"], "personal_task")
        self.assertEqual(kwargs["freeform_missing_fields"], ["deadline_local"])
        self.assertEqual(kwargs["freeform_action_hint"], "personal_task")

    async def test_invalid_task_deadline_starts_followup_instead_of_dropping_deadline(self) -> None:
        message = SimpleNamespace(chat=SimpleNamespace(id=101))
        deps = SimpleNamespace(
            llm=SimpleNamespace(
                enabled=True,
                classify_intake=AsyncMock(
                    return_value={
                        "action": "task",
                        "title": "Send report",
                        "deadline_local": "sometime soon",
                        "reply": "",
                    }
                ),
            ),
            tz_name="Europe/Moscow",
        )
        state = AsyncMock()
        state.get_state = AsyncMock(return_value=None)

        with (
            patch("bot.services.freeform_intake._load_freeform_context", AsyncMock(return_value=(None, "INBOX", [], []))),
            patch("bot.services.freeform_intake._start_followup", AsyncMock(return_value=True)) as start_followup,
        ):
            handled = await handle_freeform_text(
                message,
                deps=deps,
                db_pool=_Pool(),
                raw_text="Send report sometime soon",
                source="text",
                state=state,
            )

        self.assertTrue(handled)
        self.assertEqual(start_followup.await_count, 1)
        self.assertEqual(start_followup.await_args.kwargs["pending_action"], "task")
        self.assertEqual(start_followup.await_args.kwargs["missing_fields"], ("deadline_local",))

    async def test_personal_task_creation_writes_audit_event(self) -> None:
        gtasks = SimpleNamespace(enabled=lambda: True, create_task=AsyncMock())
        deps = SimpleNamespace(
            llm=SimpleNamespace(
                enabled=True,
                classify_intake=AsyncMock(return_value={"action": "personal_task", "title": "Buy filter", "reply": ""}),
            ),
            gtasks=gtasks,
            tz_name="Europe/Moscow",
        )
        state = AsyncMock()
        state.get_state = AsyncMock(return_value=None)
        message = SimpleNamespace(chat=SimpleNamespace(id=303))

        with (
            patch("bot.services.freeform_intake._load_freeform_context", AsyncMock(return_value=(None, "INBOX", [], []))),
            patch("bot.services.freeform_intake.get_or_create_list_id", AsyncMock(return_value="personal-list")),
            patch("bot.services.freeform_intake.db_add_event", AsyncMock()) as add_event,
            patch("bot.services.freeform_intake._rerender_with_toast", AsyncMock(return_value=1)),
        ):
            handled = await handle_freeform_text(
                message,
                deps=deps,
                db_pool=_Pool(),
                raw_text="buy filter",
                source="text",
                state=state,
            )

        self.assertTrue(handled)
        gtasks.create_task.assert_awaited_once()
        self.assertEqual(add_event.await_count, 1)
        self.assertEqual(add_event.await_args.args[1], "personal_task_created")

    async def test_idea_creation_writes_audit_event(self) -> None:
        gtasks = SimpleNamespace(enabled=lambda: True, create_task=AsyncMock())
        deps = SimpleNamespace(
            llm=SimpleNamespace(
                enabled=True,
                classify_intake=AsyncMock(return_value={"action": "idea", "idea_text": "Voice digest", "reply": ""}),
            ),
            gtasks=gtasks,
            tz_name="Europe/Moscow",
        )
        state = AsyncMock()
        state.get_state = AsyncMock(return_value=None)
        message = SimpleNamespace(chat=SimpleNamespace(id=404))

        with (
            patch("bot.services.freeform_intake._load_freeform_context", AsyncMock(return_value=(None, "INBOX", [], []))),
            patch("bot.services.freeform_intake.get_or_create_list_id", AsyncMock(return_value="ideas-list")),
            patch("bot.services.freeform_intake.db_add_event", AsyncMock()) as add_event,
            patch("bot.services.freeform_intake._rerender_with_toast", AsyncMock(return_value=1)),
        ):
            handled = await handle_freeform_text(
                message,
                deps=deps,
                db_pool=_Pool(),
                raw_text="voice digest",
                source="text",
                state=state,
            )

        self.assertTrue(handled)
        gtasks.create_task.assert_awaited_once()
        self.assertEqual(add_event.await_count, 1)
        self.assertEqual(add_event.await_args.args[1], "idea_captured")


if __name__ == "__main__":
    unittest.main()
