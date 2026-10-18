"""Telegram capture must never be lost when the model is unavailable."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot.services.freeform_intake as intake


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _Conn:
    def transaction(self):
        return _Tx()


class _Pool:
    def acquire(self):
        conn = _Conn()

        class _Acquire:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *_exc):
                return False

        return _Acquire()


def _message():
    return SimpleNamespace(chat=SimpleNamespace(id=7), message_id=501, date=None, voice=None, audio=None)


def _run_text(deps, text="позвонить подрядчику"):
    save = AsyncMock(return_value={"ok": True, "task": {"id": 1, "title": text, "project": "INBOX"}})
    toast = AsyncMock()
    with (
        patch.object(intake, "_followup_context", AsyncMock(return_value={})),
        patch.object(intake, "get_persona_mode", AsyncMock(return_value="lead")),
        patch.object(intake, "_load_freeform_context", AsyncMock(return_value=(None, None, [], []))),
        patch.object(intake, "_rerender_with_toast", toast),
        patch("bot.services.capture.save_capture", save),
    ):
        handled = asyncio.run(
            intake.handle_freeform_text(_message(), deps=deps, db_pool=_Pool(), raw_text=text)
        )
    return handled, save, toast


def test_text_without_model_goes_to_inbox_with_message_retry_key():
    handled, save, toast = _run_text(SimpleNamespace(llm=None, tz_name="Europe/Moscow"))
    assert handled is True
    kwargs = save.await_args.kwargs
    assert kwargs["text"] == "позвонить подрядчику"
    assert kwargs["client_request_id"] == "tg:7:501"
    assert "Входящие" in toast.await_args.args[3]


def test_model_error_also_goes_to_inbox():
    llm = SimpleNamespace(enabled=True, classify_intake=AsyncMock(side_effect=RuntimeError("timeout")))
    handled, save, _toast = _run_text(SimpleNamespace(llm=llm, tz_name="Europe/Moscow"))
    assert handled is True
    assert save.await_args.kwargs["text"] == "позвонить подрядчику"


def test_untranscribed_voice_stays_in_chat_and_leaves_an_inbox_reminder():
    message = _message()
    message.delete = AsyncMock()
    deps = SimpleNamespace(llm=None, tz_name="Europe/Moscow")
    save = AsyncMock(return_value={"ok": True, "task": {"id": 2, "title": "x", "project": "INBOX"}})
    with (
        patch.object(intake, "_voice_file_meta", return_value=("file", "voice.ogg", "audio/ogg", 10)),
        patch.object(intake, "_rerender_with_toast", AsyncMock()),
        patch("bot.services.capture.save_capture", save),
    ):
        handled = asyncio.run(intake.handle_freeform_voice(message, deps=deps, db_pool=_Pool()))
    assert handled is True
    assert save.await_args.kwargs["text"].startswith("Прослушать голосовое в Telegram от ")
    message.delete.assert_not_awaited()
