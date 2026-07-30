import unittest

from bot.adapters.llm_router import ResilientLLMAdapter


class FakeProvider:
    def __init__(
        self,
        *,
        result=None,
        error=None,
        startup_error=None,
        close_error=None,
        enabled=True,
        circuit_open=False,
    ):
        self.result = result
        self.error = error
        self.startup_error = startup_error
        self.close_error = close_error
        self.enabled = enabled
        self.circuit_open = circuit_open
        self.calls = []
        self.started = False
        self.closed = False

    async def startup(self):
        if self.startup_error:
            raise self.startup_error
        self.started = True

    async def close(self):
        if self.close_error:
            raise self.close_error
        self.closed = True

    async def classify_intake(self, **kwargs):
        self.calls.append(("single", kwargs))
        if self.error:
            raise self.error
        return self.result

    async def classify_intake_batch(self, **kwargs):
        self.calls.append(("batch", kwargs))
        if self.error:
            raise self.error
        return self.result

    async def transcribe_audio(self, **kwargs):
        self.calls.append(("audio", kwargs))
        if self.error:
            raise self.error
        return "текст"


class ResilientLLMAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_primary_success_does_not_call_fallback(self):
        gemini = FakeProvider(result={"action": "task", "reply": ""})
        deepseek = FakeProvider(result={"action": "idea", "reply": ""})
        router = ResilientLLMAdapter(gemini=gemini, deepseek=deepseek)

        result = await router.classify_intake(system_prompt="system", user_prompt="user")

        self.assertEqual(result["action"], "task")
        self.assertEqual(router.last_provider, "gemini")
        self.assertEqual(len(gemini.calls), 1)
        self.assertEqual(deepseek.calls, [])

    async def test_deepseek_is_used_when_gemini_fails(self):
        gemini = FakeProvider(error=RuntimeError("quota"))
        deepseek = FakeProvider(result={"actions": [{"action": "task", "title": "A"}], "reply": ""})
        router = ResilientLLMAdapter(gemini=gemini, deepseek=deepseek)

        result = await router.classify_intake_batch(system_prompt="system", user_prompt="user")

        self.assertEqual(result["actions"][0]["title"], "A")
        self.assertEqual(router.last_provider, "deepseek")
        self.assertEqual(len(gemini.calls), 1)
        self.assertEqual(len(deepseek.calls), 1)

    async def test_deepseek_can_be_the_only_text_provider(self):
        gemini = FakeProvider(enabled=False)
        deepseek = FakeProvider(result={"action": "task", "reply": ""})
        router = ResilientLLMAdapter(gemini=gemini, deepseek=deepseek)

        result = await router.classify_intake(system_prompt="system", user_prompt="user")

        self.assertEqual(result["action"], "task")
        self.assertEqual(router.last_provider, "deepseek")
        self.assertTrue(router.enabled)
        self.assertFalse(router.transcription_enabled)

    async def test_audio_remains_gemini_only(self):
        gemini = FakeProvider(enabled=False)
        deepseek = FakeProvider(result={})
        router = ResilientLLMAdapter(gemini=gemini, deepseek=deepseek)

        with self.assertRaisesRegex(RuntimeError, "GEMINI_API_KEY"):
            await router.transcribe_audio(audio_bytes=b"x", filename="a.ogg")

    async def test_gemini_startup_failure_does_not_block_deepseek(self):
        gemini = FakeProvider(startup_error=RuntimeError("gemini session failed"))
        deepseek = FakeProvider(result={"action": "task"})
        router = ResilientLLMAdapter(gemini=gemini, deepseek=deepseek)

        await router.startup()

        self.assertFalse(gemini.started)
        self.assertTrue(deepseek.started)

    async def test_close_attempts_both_providers(self):
        gemini = FakeProvider(close_error=RuntimeError("close failed"))
        deepseek = FakeProvider()
        router = ResilientLLMAdapter(gemini=gemini, deepseek=deepseek)

        await router.close()

        self.assertTrue(deepseek.closed)


if __name__ == "__main__":
    unittest.main()
