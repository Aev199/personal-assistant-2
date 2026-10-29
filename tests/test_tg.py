"""Thin Telegram client on the shared capture core."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot.tg as tg
from bot.services.task_actions import due_for


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _Conn:
    def transaction(self):
        return _Tx()


class _Pool:
    def __init__(self):
        self.conn = _Conn()

    def acquire(self):
        conn = self.conn

        class _Acquire:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *_exc):
                return False

        return _Acquire()


def _message(text="позвонить подрядчику", **extra):
    msg = SimpleNamespace(
        chat=SimpleNamespace(id=7), message_id=42, text=text, voice=None, audio=None,
        date=datetime(2026, 10, 19, 11, 5, tzinfo=timezone.utc), bot=SimpleNamespace(),
        delete=AsyncMock(), **extra,
    )
    return msg


DEPS = SimpleNamespace(tz_name="Europe/Moscow", icloud=None, llm=None, admin_id=1)
SAVED = {"ok": True, "status": "saved", "saved": [{"status": "saved", "kind": "task", "title": "Позвонить подрядчику"}],
         "needs_input": [], "pending": []}


def _run_text(result, message=None, question=None):
    message = message or _message()
    capture = AsyncMock(return_value=result)
    today = AsyncMock(return_value=1)
    render = AsyncMock(return_value=1)
    with (
        patch.object(tg, "process_native_capture", capture),
        patch.object(tg.simple_daily, "ui_render_today", today),
        patch.object(tg, "ui_render", render),
        patch.object(tg, "get_conversation_state", AsyncMock(return_value=question)),
        patch.object(tg, "clear_conversation_state", AsyncMock()),
        patch.object(tg, "set_conversation_state", AsyncMock()) as remember,
    ):
        asyncio.run(tg.on_text(message, _Pool(), DEPS))
    return message, capture, today, render, remember


def test_text_uses_shared_capture_core_and_offers_undo():
    message, capture, today, _render, _ = _run_text(SAVED)
    kwargs = capture.await_args.kwargs
    assert kwargs["source"] == "telegram"
    assert kwargs["capture_id"] == "tg-7-42"
    assert kwargs["prepend_text"] is None
    notice = today.await_args.kwargs["notice"]
    assert "✓ Задача: Позвонить подрядчику" in notice
    assert today.await_args.kwargs["extra_rows"][0][0].callback_data == "cap:undo"
    message.delete.assert_awaited_once()


def test_inbox_fallback_is_reported_as_inbox():
    result = {**SAVED, "inbox_fallback": True}
    _message_, _capture, today, _render, _ = _run_text(result)
    assert today.await_args.kwargs["notice"].startswith("📥 Во Входящие")


def test_unsaved_text_stays_in_chat():
    message, _capture, today, _render, _ = _run_text({"ok": True, "status": "stored", "saved": []})
    message.delete.assert_not_awaited()
    assert "осталось в чате" in today.await_args.kwargs["notice"]


def test_question_is_remembered_and_answer_carries_context():
    question_result = {"ok": True, "status": "needs_input", "saved": [], "pending": [], "context": "напомни позвонить",
                       "needs_input": [{"prompt": "Когда напомнить?"}]}
    _message_, _capture, _today, render, remember = _run_text(question_result)
    assert remember.await_args.kwargs["payload"]["context"] == "напомни позвонить"
    assert "Когда напомнить?" in render.await_args.kwargs["text"]
    assert render.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data == "cap:skip"

    answer = _message("завтра в 10")
    _m, capture, _t, _r, _ = _run_text(SAVED, message=answer, question={"payload": {"context": "напомни позвонить"}})
    assert capture.await_args.kwargs["prepend_text"] == "напомни позвонить"


def test_calendar_event_waits_for_one_tap_confirmation():
    pending = {"ok": True, "status": "needs_confirmation", "saved": [], "needs_input": [],
               "pending": [{"pending_action_id": 5, "title": "Встреча с Ивановым",
                            "payload": {"start_local": "2026-10-20T15:00:00+03:00"}}]}
    _m, _c, _t, render, _ = _run_text(pending)
    assert "Встреча с Ивановым · 20.10 15:00" in render.await_args.kwargs["text"]
    buttons = [b.callback_data for b in render.await_args.kwargs["reply_markup"].inline_keyboard[0]]
    assert buttons == ["cap:ok:5", "cap:no:5"]


def test_undo_word_undoes_without_capturing():
    capture = AsyncMock()
    undo = AsyncMock(return_value="↩️ Отменено: X")
    today = AsyncMock(return_value=1)
    with (
        patch.object(tg, "process_native_capture", capture),
        patch.object(tg, "undo_latest", undo),
        patch.object(tg.simple_daily, "ui_render_today", today),
    ):
        asyncio.run(tg.on_text(_message("отмени"), _Pool(), DEPS))
    capture.assert_not_awaited()
    assert today.await_args.kwargs["notice"] == "↩️ Отменено: X"


def test_voice_without_model_stays_and_leaves_inbox_reminder():
    message = _message(None)
    message.voice = SimpleNamespace(file_id="f", mime_type="audio/ogg")
    save = AsyncMock(return_value={"ok": True, "task": {"id": 1}})
    with (
        patch.object(tg, "save_capture", save),
        patch.object(tg.simple_daily, "ui_render_today", AsyncMock(return_value=1)),
    ):
        asyncio.run(tg.on_voice(message, _Pool(), DEPS))
    assert save.await_args.kwargs["text"] == "Прослушать голосовое в Telegram от 19.10 14:05"
    message.delete.assert_not_awaited()


def test_voice_transcript_goes_through_the_same_capture():
    llm = SimpleNamespace(enabled=True, transcribe_audio=AsyncMock(return_value=" позвонить  в банк "))
    deps = SimpleNamespace(**{**DEPS.__dict__, "llm": llm})
    message = _message(None)
    message.voice = SimpleNamespace(file_id="f", mime_type="audio/ogg")
    message.bot = SimpleNamespace(download=AsyncMock())
    capture = AsyncMock(return_value=SAVED)
    with (
        patch.object(tg, "process_native_capture", capture),
        patch.object(tg, "ui_render", AsyncMock()),
        patch.object(tg, "get_conversation_state", AsyncMock(return_value=None)),
        patch.object(tg.simple_daily, "ui_render_today", AsyncMock(return_value=1)),
    ):
        asyncio.run(tg.on_voice(message, _Pool(), deps))
    assert capture.await_args.kwargs["text"] == "позвонить в банк"
    assert capture.await_args.kwargs["source"] == "telegram-voice"
    message.delete.assert_awaited_once()


def test_card_buttons_complete_plan_and_set_due():
    callback = SimpleNamespace(data="tc:9:due:tomorrow", message=_message(), answer=AsyncMock())
    due = AsyncMock()
    with (
        patch.object(tg, "set_due", due),
        patch.object(tg, "_render_card", AsyncMock()) as card,
    ):
        asyncio.run(tg.cb_task_card(callback, _Pool(), DEPS))
    assert due.await_args.kwargs["choice"] == "tomorrow"
    assert card.await_args.kwargs["notice"] == "Срок изменён"

    callback.data = "tc:9:done"
    with (
        patch.object(tg, "complete_task", AsyncMock(return_value="Сделать отчёт")),
        patch.object(tg.simple_daily, "ui_render_today", AsyncMock(return_value=1)) as today,
    ):
        asyncio.run(tg.cb_task_card(callback, _Pool(), DEPS))
    assert today.await_args.kwargs["notice"] == "✓ Готово: Сделать отчёт"


def test_due_choices_land_at_six_pm_local():
    now = datetime(2026, 10, 19, 9, 0, tzinfo=timezone.utc)  # 12:00 in Moscow
    assert due_for("today", tz_name="Europe/Moscow", now=now) == datetime(2026, 10, 19, 15, 0)
    assert due_for("tomorrow", tz_name="Europe/Moscow", now=now) == datetime(2026, 10, 20, 15, 0)
    assert due_for("week", tz_name="Europe/Moscow", now=now) == datetime(2026, 10, 26, 15, 0)
    assert due_for("none", tz_name="Europe/Moscow", now=now) is None
    late = datetime(2026, 10, 19, 17, 0, tzinfo=timezone.utc)  # 20:00 in Moscow
    assert due_for("today", tz_name="Europe/Moscow", now=late) == datetime(2026, 10, 19, 20, 0)


def test_only_thin_handlers_are_registered_and_old_buttons_fall_back_to_today():
    from aiogram import Dispatcher

    dp = Dispatcher()
    tg.register(dp)
    callbacks = [h.callback.__name__ for h in dp.callback_query.handlers]
    messages = [h.callback.__name__ for h in dp.message.handlers]
    assert callbacks[-1] == "cb_stale"
    assert messages[-2:] == ["on_voice", "on_text"]
    for gone in ("cb_add_event_start", "cb_bulk", "cb_nav_projects", "cb_nav_team"):
        assert gone not in callbacks
