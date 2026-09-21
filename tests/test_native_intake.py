import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.services.freeform_intake import IntakeIntent
from bot.services.native_intake import _classify, process_native_capture


class _FakeLLM:
    enabled = True

    def __init__(self, payload=None):
        self.payload = payload or {"action": "task", "title": "Проверить расчёт", "reply": ""}
        self.single_calls = 0
        self.batch_calls = 0
        self.last_provider = "gemini"

    async def classify_intake(self, **_kwargs):
        self.single_calls += 1
        return self.payload

    async def classify_intake_batch(self, **_kwargs):
        self.batch_calls += 1
        return {"actions": [self.payload], "reply": ""}


class NativeIntakeTests(unittest.IsolatedAsyncioTestCase):
    async def test_plain_text_uses_shared_llm_classifier(self):
        llm = _FakeLLM()
        deps = SimpleNamespace(llm=llm, tz_name="Europe/Moscow")

        intents = await _classify(
            "проверить расчёт",
            deps=deps,
            current_project_code="INBOX",
            projects=[],
            team=[],
        )

        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0].action, "task")
        self.assertEqual(intents[0].title, "Проверить расчёт")
        self.assertEqual(llm.single_calls, 1)

    async def test_explicit_idea_stays_fast_and_does_not_need_llm(self):
        llm = _FakeLLM()
        deps = SimpleNamespace(llm=llm, tz_name="Europe/Moscow")

        intents = await _classify(
            "идея: автоматизировать отчёт",
            deps=deps,
            current_project_code="INBOX",
            projects=[],
            team=[],
        )

        self.assertEqual(intents[0].action, "idea")
        self.assertEqual(intents[0].idea_text, "автоматизировать отчёт")
        self.assertEqual(llm.single_calls, 0)

    async def test_raw_capture_is_retained_when_llm_is_unavailable(self):
        deps = SimpleNamespace(llm=None, tz_name="Europe/Moscow")
        save = AsyncMock()

        with (
            patch("bot.services.native_intake._save_receipt", save),
            patch(
                "bot.services.native_intake._load_context",
                AsyncMock(return_value=("lead", None, "INBOX", [], [])),
            ),
        ):
            result = await process_native_capture(
                text="важная мысль",
                deps=deps,
                db_pool=object(),
                chat_id=42,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "stored")
        self.assertGreaterEqual(save.await_count, 2)

    async def test_incomplete_intent_returns_one_concrete_question(self):
        deps = SimpleNamespace(llm=SimpleNamespace(last_provider="gemini"), tz_name="Europe/Moscow")
        intent = IntakeIntent(
            action="reply",
            needs_followup=True,
            followup_action="reminder",
            followup_prompt="Когда напомнить?",
            missing_fields=("remind_at_local",),
        )

        with (
            patch("bot.services.native_intake._save_receipt", AsyncMock()),
            patch(
                "bot.services.native_intake._load_context",
                AsyncMock(return_value=("lead", None, "INBOX", [], [])),
            ),
            patch("bot.services.native_intake._classify", AsyncMock(return_value=[intent])),
        ):
            result = await process_native_capture(
                text="напомни позвонить",
                deps=deps,
                db_pool=object(),
                chat_id=42,
            )

        self.assertEqual(result["status"], "needs_input")
        self.assertEqual(result["needs_input"][0]["prompt"], "Когда напомнить?")
        self.assertEqual(result["saved"], [])


if __name__ == "__main__":
    unittest.main()
