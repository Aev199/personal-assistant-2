import asyncio
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


class _FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _FakeConn:
    def transaction(self):
        return _FakeTransaction()


class _FakePool:
    def acquire(self):
        conn = _FakeConn()

        class _Acquire:
            async def __aenter__(self_inner):
                return conn

            async def __aexit__(self_inner, *_exc):
                return False

        return _Acquire()


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
            patch("bot.services.native_intake._load_receipt", AsyncMock(return_value=None)),
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

    async def _capture_without_model(self, *, classify, capture_result=None, capture_error=None):
        save_receipt = AsyncMock()
        save_capture = AsyncMock(
            return_value=capture_result
            or {"ok": True, "task": {"id": 77, "title": "важная мысль", "project": "INBOX"}},
            side_effect=capture_error,
        )
        with (
            patch("bot.services.native_intake._save_receipt", save_receipt),
            patch("bot.services.native_intake._load_receipt", AsyncMock(return_value=None)),
            patch(
                "bot.services.native_intake._load_context",
                AsyncMock(return_value=("lead", None, "INBOX", [], [])),
            ),
            patch("bot.services.native_intake._classify", classify),
            patch("bot.services.native_intake.save_capture", save_capture),
        ):
            result = await process_native_capture(
                text="важная мысль",
                deps=SimpleNamespace(llm=None, tz_name="Europe/Moscow"),
                db_pool=_FakePool(),
                chat_id=42,
                capture_id="abc-123",
            )
        return result, save_capture, save_receipt

    async def test_model_failure_puts_text_into_visible_inbox(self):
        result, save_capture, save_receipt = await self._capture_without_model(
            classify=AsyncMock(side_effect=RuntimeError("llm_unavailable")),
        )

        self.assertEqual(result["status"], "saved")
        self.assertTrue(result["inbox_fallback"])
        self.assertEqual(result["saved"][0]["kind"], "task")
        self.assertEqual(result["saved"][0]["task_id"], 77)
        kwargs = save_capture.await_args.kwargs
        self.assertEqual(kwargs["text"], "важная мысль")
        # The client capture id is the shared retry key: a retry cannot duplicate.
        self.assertEqual(kwargs["client_request_id"], "native:abc-123")
        # The final receipt stores the saved result for idempotent retries.
        self.assertEqual(save_receipt.await_args.kwargs["result"], result)

    async def test_slow_model_does_not_keep_capture_waiting(self):
        async def slow(*_args, **_kwargs):
            await asyncio.sleep(5)

        with patch.dict("os.environ", {"NATIVE_INTAKE_CLASSIFY_TIMEOUT_SEC": "1"}):
            started = asyncio.get_running_loop().time()
            result, _save_capture, save_receipt = await self._capture_without_model(classify=slow)
            elapsed = asyncio.get_running_loop().time() - started

        self.assertLess(elapsed, 3)
        self.assertEqual(result["status"], "saved")
        self.assertEqual(save_receipt.await_args.kwargs["items"][0]["error"], "classify_timeout")

    async def test_inbox_failure_still_keeps_raw_capture_for_retry(self):
        result, _save_capture, save_receipt = await self._capture_without_model(
            classify=AsyncMock(side_effect=RuntimeError("llm_unavailable")),
            capture_error=RuntimeError("db down"),
        )

        self.assertEqual(result["status"], "stored")
        self.assertEqual(save_receipt.await_args.kwargs["result"]["status"], "stored")

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
            patch("bot.services.native_intake._load_receipt", AsyncMock(return_value=None)),
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


    async def test_processed_capture_id_is_idempotent(self):
        cached = {
            "ok": True,
            "capture_id": "abc-123",
            "status": "saved",
            "saved": [{"status": "saved", "kind": "task", "title": "X"}],
            "needs_input": [],
            "pending": [],
        }
        classify = AsyncMock()

        with (
            patch("bot.services.native_intake._load_receipt", AsyncMock(return_value=cached)),
            patch("bot.services.native_intake._classify", classify),
        ):
            result = await process_native_capture(
                text="задача X",
                deps=SimpleNamespace(llm=None, tz_name="Europe/Moscow"),
                db_pool=object(),
                chat_id=42,
                capture_id="abc-123",
            )

        self.assertEqual(result, cached)
        classify.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
