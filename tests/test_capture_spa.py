import copy
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText
from bot.services import freeform_intake as intake
from bot.services.capture_receipts import render_receipt
from bot.services.pending_actions import create_pending_preview
from bot.ui.render import ui_render


class Pool:
    def __init__(self, conn):
        self.conn = conn
    def acquire(self):
        return self
    async def __aenter__(self):
        return self.conn
    async def __aexit__(self, *args):
        return False


def message():
    return SimpleNamespace(chat=SimpleNamespace(id=1), message_id=99,
                           answer=AsyncMock(), delete=AsyncMock(), bot=SimpleNamespace())


class CaptureTests(unittest.IsolatedAsyncioTestCase):
    def test_normalizer_keeps_incomplete_reminder_and_maps_title_field(self):
        intents, _ = intake._normalize_batch_payloads({"actions": [
            {"action": "task", "title": "Расчёт"},
            {"action": "reminder", "title": "Позвонить Ивану"},
            {"action": "personal_task", "title": "Купить хлеб"},
        ]}, retain_incomplete=True)
        self.assertEqual(len(intents), 3)
        self.assertTrue(intents[1].needs_followup)
        self.assertEqual(intents[1].followup_action, "reminder")
        self.assertEqual(intents[1].reminder_text, "Позвонить Ивану")
        self.assertEqual(intents[2].action, "personal_task")

    async def run_batch(self, intents, outcomes, classify_error=False):
        msg = message()
        deps = SimpleNamespace(llm=SimpleNamespace(classify_intake_batch=AsyncMock(
            side_effect=RuntimeError("offline") if classify_error else None, return_value={})))
        saves = []
        async def save(pool, chat, capture_id, receipt):
            saves.append(copy.deepcopy(receipt))
        with (
            patch.object(intake, '_normalize_batch_payloads', return_value=(intents, "")),
            patch.object(intake, '_execute_pending_intent', AsyncMock(side_effect=outcomes)),
            patch.object(intake, '_clear_followup_state', AsyncMock()),
            patch('bot.services.capture_receipts.save_receipt', side_effect=save),
            patch('bot.services.capture_receipts.render_receipt', AsyncMock()) as render,
        ):
            ok = await intake._handle_batch_intents(msg, deps=deps, db_pool=object(), state=None,
                text="Расчёт завтра; купить хлеб; напомнить Ивану", source="voice", prepend_text=None,
                tz=ZoneInfo("Europe/Moscow"), tz_name="Europe/Moscow", current_project_id=None,
                current_project_code=None, projects=[], team=[], persona_mode="solo",
                followup_data={}, action_hint=None)
        self.assertTrue(ok)
        msg.answer.assert_not_awaited()
        render.assert_awaited_once()
        return saves

    async def test_mixed_batch_preserves_incomplete_and_personal_items(self):
        intents = [intake.IntakeIntent(action="task", title="Расчёт"),
                   intake.IntakeIntent(action="personal_task", title="Купить хлеб"),
                   intake.IntakeIntent(action="reminder", reminder_text="Напомнить Ивану", needs_followup=True)]
        saves = await self.run_batch(intents, [10, False, False])
        self.assertEqual(saves[0]['items'], [])  # Raw input durable before side effects.
        self.assertEqual(len(saves[-1]['items']), 3)
        self.assertEqual(saves[-1]['items'][0]['pending_action_id'], 10)
        self.assertEqual(saves[-1]['items'][1]['intent']['action'], 'personal_task')
        self.assertEqual(saves[-1]['items'][2]['intent']['reminder_text'], 'Напомнить Ивану')
        self.assertIn('купить хлеб', saves[-1]['raw_text'])

    async def test_partial_failure_keeps_both_success_and_failed_item(self):
        saves = await self.run_batch([intake.IntakeIntent(action='task', title='A'),
                                     intake.IntakeIntent(action='task', title='B')], [7, RuntimeError('failed')])
        self.assertEqual(saves[-1]['items'][0]['pending_action_id'], 7)
        self.assertNotIn('pending_action_id', saves[-1]['items'][1])

    async def test_classification_failure_retains_source_without_single_item_retry(self):
        saves = await self.run_batch([], [], classify_error=True)
        self.assertEqual(len(saves), 1)
        self.assertTrue(saves[0]['raw_text'])

    async def test_batch_draft_does_not_send_or_render_a_separate_card(self):
        msg = message()
        with (
            patch('bot.services.pending_actions.create_pending_action', AsyncMock(return_value=11)),
            patch('bot.services.pending_actions.remember_recent_action', AsyncMock()),
            patch('bot.services.pending_actions.ui_render', AsyncMock()) as render,
        ):
            result = await create_pending_preview(msg, db_pool=Pool(object()), deps=SimpleNamespace(tz_name='UTC'),
                kind='event', payload={}, fingerprint='x', summary='Meeting', source='text.batch0', force_new=True)
        self.assertEqual(result, 11)
        msg.answer.assert_not_awaited()
        render.assert_not_awaited()

    async def test_receipt_uses_real_statuses_and_preserves_original_pagination(self):
        raw = '<&😀>' * 300
        receipt = {'raw_text': raw, 'items': [
            {'intent': {'action': 'task', 'title': '<Расчёт>'}, 'pending_action_id': 1},
            {'intent': {'action': 'event', 'title': 'Встреча'}, 'pending_action_id': 2},
            {'intent': {'action': 'personal_task', 'title': 'Аптека'}},
            {'intent': {'action': 'task', 'title': 'Письмо'}, 'pending_action_id': 3},
        ]}
        conn = SimpleNamespace(fetch=AsyncMock(return_value=[
            {'id': 1, 'status': 'executed', 'expires_at': None},
            {'id': 2, 'status': 'pending', 'expires_at': datetime.now(timezone.utc)+timedelta(hours=1)},
            {'id': 3, 'status': 'failed', 'expires_at': None},
        ]))
        with (
            patch('bot.services.capture_receipts.load_receipt', AsyncMock(return_value=receipt)),
            patch('bot.services.capture_receipts.ui_render', AsyncMock()) as render,
        ):
            await render_receipt(message(), Pool(conn), 'abc')
        args = render.await_args.kwargs
        self.assertIn('Создано: 1', args['text'])
        self.assertIn('Подтвердить: 1', args['text'])
        self.assertIn('Не обработано: 1', args['text'])
        self.assertIn('Ошибка: 1', args['text'])
        self.assertIn('&lt;Расчёт&gt;', args['text'])
        self.assertIn('<details>', args['rich_html'])
        self.assertIn('<blockquote expandable>', args['text'])
        self.assertLess(len(args['text'].encode('utf-16-le'))//2, 4032)
        callbacks = [b.callback_data for row in args['reply_markup'].inline_keyboard for b in row]
        self.assertIn('capture:draft:abc:2', callbacks)
        self.assertIn('capture:source:abc:1', callbacks)
        self.assertNotIn('llm:batch_confirm', callbacks)  # Never confirms unrelated pending actions.

    async def test_voice_progress_stays_in_spa_and_failed_transcript_is_not_deleted(self):
        msg = message()
        msg.bot.download = AsyncMock()
        deps = SimpleNamespace(llm=SimpleNamespace(enabled=True, transcribe_audio=AsyncMock(return_value="")))
        with (
            patch.object(intake, '_voice_file_meta', return_value=('file', 'voice.ogg', 'audio/ogg', 10)),
            patch.object(intake, '_rerender_with_toast', AsyncMock()),
            patch('bot.ui.render.ui_render', AsyncMock()) as render,
        ):
            await intake.handle_freeform_voice(msg, deps=deps, db_pool=object())
        render.assert_awaited_once()
        msg.answer.assert_not_awaited()
        msg.delete.assert_not_awaited()


class RichSpaTests(unittest.IsolatedAsyncioTestCase):
    async def test_rich_rejection_edits_same_anchor_as_html(self):
        bot = SimpleNamespace(edit_message_text=AsyncMock(side_effect=[
            TelegramBadRequest(method=EditMessageText(chat_id=1, message_id=42, text='x'), message='unsupported rich'),
            SimpleNamespace(message_id=42),
        ]), send_message=AsyncMock(), send_rich_message=AsyncMock(), delete_message=AsyncMock())
        with (
            patch('bot.ui.render.ui_get_state', AsyncMock(return_value={'ui_message_id': 42})),
            patch('bot.ui.render.ui_set_state', AsyncMock()) as persist,
        ):
            result = await ui_render(bot=bot, db_pool=Pool(object()), chat_id=1, text='<b>Saved</b>',
                                     rich_html='<p>Saved</p>', reply_markup=None)
        self.assertEqual(result, 42)
        calls = bot.edit_message_text.await_args_list
        self.assertEqual([c.kwargs['message_id'] for c in calls], [42, 42])
        self.assertIn('rich_message', calls[0].kwargs)
        self.assertNotIn('text', calls[0].kwargs)
        self.assertEqual(calls[1].kwargs['text'], '<b>Saved</b>')
        bot.send_message.assert_not_awaited()
        bot.send_rich_message.assert_not_awaited()
        bot.delete_message.assert_not_awaited()
        self.assertEqual(persist.await_args.kwargs['ui_message_id'], 42)

    async def test_old_aiogram_uses_html_without_rich_calls(self):
        bot = SimpleNamespace(edit_message_text=AsyncMock(), send_message=AsyncMock(), delete_message=AsyncMock())
        with (
            patch('bot.ui.render.ui_get_state', AsyncMock(return_value={'ui_message_id': 42})),
            patch('bot.ui.render.ui_set_state', AsyncMock()),
            patch('bot.ui.render.InputRichMessage', None),
        ):
            await ui_render(bot=bot, db_pool=Pool(object()), chat_id=1, text='Saved',
                            rich_html='<p>Saved</p>', reply_markup=None)
        self.assertNotIn('rich_message', bot.edit_message_text.await_args.kwargs)
        bot.send_message.assert_not_awaited()

    async def test_rich_send_failure_creates_only_one_fallback_anchor(self):
        bot = SimpleNamespace(edit_message_text=AsyncMock(), send_message=AsyncMock(return_value=SimpleNamespace(message_id=55)),
                              send_rich_message=AsyncMock(side_effect=TelegramBadRequest(
                                  method=EditMessageText(chat_id=1, message_id=42, text='x'), message='unsupported rich')),
                              delete_message=AsyncMock())
        with (
            patch('bot.ui.render.ui_get_state', AsyncMock(return_value={})),
            patch('bot.ui.render.ui_set_state', AsyncMock()) as persist,
        ):
            result = await ui_render(bot=bot, db_pool=Pool(object()), chat_id=1, text='Saved',
                                     rich_html='<p>Saved</p>', reply_markup=None)
        self.assertEqual(result, 55)
        bot.send_rich_message.assert_awaited_once()
        bot.send_message.assert_awaited_once()
        self.assertEqual(persist.await_args.kwargs['ui_message_id'], 55)

    async def test_existing_spa_draft_is_never_deleted_on_confirmation(self):
        from bot.handlers.pending_actions import _delete_legacy_preview
        msg = message()
        with patch('bot.handlers.pending_actions.ui_get_state', AsyncMock(return_value={'ui_message_id': 99})):
            await _delete_legacy_preview(SimpleNamespace(message=msg), Pool(object()))
        msg.delete.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
