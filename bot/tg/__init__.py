"""Thin Telegram client: write anything, see today, finish tasks.

Telegram uses the same capture core as the iPhone and the web interface
(`process_native_capture`): Gemini parses the text, and without a timely model
answer the text still lands in Inbox. Screens: «Сегодня» (home), «Все дела»,
a task card, ideas and help. Reminder pop-ups and the «На сегодня» picker keep
their own small handlers.
"""
from __future__ import annotations

import io
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import asyncpg
from aiogram import Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.db.runtime_state import clear_conversation_state, get_conversation_state, set_conversation_state
from bot.deps import AppDeps
from bot.services.capture import save_capture
from bot.services.day_plan import DayPlanError, local_today, set_planned
from bot.services.native_intake import cancel_native_action, confirm_native_action, process_native_capture
from bot.services.task_actions import DUE_CHOICES, TaskActionError, complete_task, set_due
from bot.services.undo import undo_latest
from bot.tz import resolve_tz_name
from bot.ui import simple_daily
from bot.ui.render import ui_render
from bot.ui.state import _ui_payload_get, ui_get_state
from bot.utils import h, try_delete_user_message

logger = logging.getLogger(__name__)

QUESTION_FLOW = "tg_capture_question"
QUESTION_TTL_SEC = 15 * 60
UNDO_WORDS = {"отмени", "отмена", "отменить", "undo"}
KIND_LABELS = {
    "task": "Задача",
    "personal_task": "Личное",
    "reminder": "Напоминание",
    "idea": "Идея",
    "event": "Встреча",
}


# ── rendering ───────────────────────────────────────────────────────────────

async def show_today(
    message: Message,
    db_pool: asyncpg.Pool,
    deps: AppDeps,
    *,
    notice: str | None = None,
    extra_rows: list[list[InlineKeyboardButton]] | None = None,
    edit: bool = False,
) -> int:
    return await simple_daily.ui_render_today(
        message,
        db_pool,
        tz_name=deps.tz_name,
        icloud=deps.icloud,
        notice=notice,
        extra_rows=extra_rows,
        preferred_message_id=int(message.message_id) if edit else None,
    )


async def _show_progress(message: Message, db_pool: asyncpg.Pool, text: str) -> None:
    try:
        await ui_render(
            bot=message.bot,
            db_pool=db_pool,
            chat_id=int(message.chat.id),
            text=text,
            reply_markup=None,
            fallback_message=message,
            parse_mode=None,
        )
    except Exception:
        logger.exception("progress render failed")


def _result_lines(result: dict) -> list[str]:
    lines: list[str] = []
    if result.get("inbox_fallback"):
        for item in result.get("saved") or []:
            lines.append(f"📥 Во Входящие: {h(item.get('title') or '')}")
        return lines
    for item in result.get("saved") or []:
        label = KIND_LABELS.get(str(item.get("kind") or ""), "Записано")
        prefix = "Уже записано" if item.get("status") == "duplicate" else f"✓ {label}"
        lines.append(f"{prefix}: {h(item.get('title') or '')}")
    return lines


def _pending_text(item: dict, tz_name: str) -> str:
    title = h(item.get("title") or "Встреча")
    payload = item.get("payload") or {}
    start = str(payload.get("start_local") or "").strip()
    if start:
        try:
            when = datetime.fromisoformat(start.replace("Z", "+00:00"))
            if when.tzinfo is not None:
                when = when.astimezone(ZoneInfo(resolve_tz_name(tz_name)))
            title += f" · {when:%d.%m %H:%M}"
        except ValueError:
            pass
    return f"📅 Добавить в календарь: {title}?"


async def show_capture_result(message: Message, db_pool: asyncpg.Pool, deps: AppDeps, result: dict) -> None:
    chat_id = int(message.chat.id)
    lines = _result_lines(result)

    if result.get("needs_input"):
        question = result["needs_input"][0]
        async with db_pool.acquire() as conn:
            await set_conversation_state(
                conn,
                chat_id,
                QUESTION_FLOW,
                step="waiting",
                payload={"context": result.get("context") or "", "prompt": question.get("prompt") or ""},
                ttl_sec=QUESTION_TTL_SEC,
            )
        text = "\n".join(lines + ([""] if lines else []) + [f"❓ {h(question.get('prompt') or 'Нужно уточнение.')}", "Ответьте сообщением."])
        await ui_render(
            bot=message.bot,
            db_pool=db_pool,
            chat_id=chat_id,
            text=text,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Не уточнять", callback_data="cap:skip")]]
            ),
            screen="capture_question",
            fallback_message=message,
            parse_mode="HTML",
        )
        return

    if result.get("pending"):
        item = result["pending"][0]
        pending_id = int(item["pending_action_id"])
        text = "\n".join(lines + ([""] if lines else []) + [_pending_text(item, deps.tz_name)])
        await ui_render(
            bot=message.bot,
            db_pool=db_pool,
            chat_id=chat_id,
            text=text,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[
                    InlineKeyboardButton(text="Добавить", callback_data=f"cap:ok:{pending_id}"),
                    InlineKeyboardButton(text="Не добавлять", callback_data=f"cap:no:{pending_id}"),
                ]]
            ),
            screen="capture_confirm",
            fallback_message=message,
            parse_mode="HTML",
        )
        return

    if not lines:
        lines = [h(result.get("message") or "Записано.")]
    undo_row = [[InlineKeyboardButton(text="↩️ Отменить", callback_data="cap:undo")]]
    can_undo = any(item.get("status") == "saved" for item in result.get("saved") or [])
    await show_today(message, db_pool, deps, notice="\n".join(lines), extra_rows=undo_row if can_undo else None)


# ── capture ─────────────────────────────────────────────────────────────────

async def capture_text(message: Message, db_pool: asyncpg.Pool, deps: AppDeps, text: str, *, source: str) -> bool:
    """Returns False only when nothing could be saved (the message is kept)."""
    chat_id = int(message.chat.id)
    async with db_pool.acquire() as conn:
        question = await get_conversation_state(conn, chat_id, QUESTION_FLOW)
        if question:
            await clear_conversation_state(conn, chat_id, QUESTION_FLOW)
    context = ((question or {}).get("payload") or {}).get("context") or None

    result = await process_native_capture(
        text=text,
        deps=deps,
        db_pool=db_pool,
        chat_id=chat_id,
        prepend_text=context,
        source=source,
        capture_id=f"tg-{chat_id}-{int(message.message_id)}",
    )
    if not result.get("ok") or result.get("status") == "stored":
        await show_today(message, db_pool, deps, notice="⚠️ Сейчас не удалось сохранить. Сообщение осталось в чате — отправьте его ещё раз чуть позже.")
        return False
    await show_capture_result(message, db_pool, deps, result)
    return True


async def on_text(message: Message, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    text = (message.text or "").strip()
    if not text:
        return
    if text.startswith("/"):
        await try_delete_user_message(message)
        await show_today(message, db_pool, deps, notice="Такой команды нет. Просто напишите, что нужно не забыть.")
        return
    if text.lower() in UNDO_WORDS:
        await try_delete_user_message(message)
        await undo(message, db_pool, deps)
        return

    await _show_progress(message, db_pool, "⏳ Записываю…")
    if await capture_text(message, db_pool, deps, text, source="telegram"):
        await try_delete_user_message(message)


def _voice_meta(message: Message) -> tuple[str | None, str, str]:
    if message.voice:
        return message.voice.file_id, "voice.ogg", message.voice.mime_type or "audio/ogg"
    if message.audio:
        return message.audio.file_id, message.audio.file_name or "audio.mp3", message.audio.mime_type or "audio/mpeg"
    return None, "", ""


async def _keep_voice_for_later(message: Message, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    """The voice stays in the chat; Inbox gets a reminder to listen to it."""
    chat_id = int(message.chat.id)
    sent = message.date or datetime.now(timezone.utc)
    when = sent.astimezone(ZoneInfo(resolve_tz_name(deps.tz_name))).strftime("%d.%m %H:%M")
    try:
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                await save_capture(
                    conn,
                    chat_id=chat_id,
                    text=f"Прослушать голосовое в Telegram от {when}",
                    client_request_id=f"tg-voice:{chat_id}:{int(message.message_id)}",
                    source="telegram",
                )
        notice = "🎤 Голосовое не распознано. Оно осталось в чате, а во Входящих есть напоминание его прослушать."
    except Exception:
        logger.exception("voice inbox reminder failed")
        notice = "🎤 Голосовое не распознано и осталось в чате."
    await show_today(message, db_pool, deps, notice=notice)


async def on_voice(message: Message, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    file_id, filename, mime_type = _voice_meta(message)
    if not file_id:
        return
    llm = deps.llm
    if llm is None or not getattr(llm, "enabled", False):
        await _keep_voice_for_later(message, db_pool, deps)
        return

    await _show_progress(message, db_pool, "🎤 Распознаю…")
    try:
        buffer = io.BytesIO()
        await message.bot.download(file_id, destination=buffer)
        transcript = " ".join(str(await llm.transcribe_audio(
            audio_bytes=buffer.getvalue(), filename=filename, mime_type=mime_type,
        ) or "").split())
    except Exception:
        logger.exception("voice transcription failed")
        transcript = ""
    if not transcript:
        await _keep_voice_for_later(message, db_pool, deps)
        return

    if await capture_text(message, db_pool, deps, transcript, source="telegram-voice"):
        await try_delete_user_message(message)


async def undo(message: Message, db_pool: asyncpg.Pool, deps: AppDeps, *, edit: bool = False) -> None:
    try:
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                notice = await undo_latest(conn, chat_id=int(message.chat.id), icloud=deps.icloud)
    except Exception:
        logger.exception("undo failed")
        notice = "Не удалось отменить."
    await show_today(message, db_pool, deps, notice=notice, edit=edit)


async def cb_capture(callback: CallbackQuery, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    message = callback.message
    chat_id = int(message.chat.id)
    await callback.answer()

    if action == "undo":
        await undo(message, db_pool, deps, edit=True)
        return
    if action == "skip":
        async with db_pool.acquire() as conn:
            await clear_conversation_state(conn, chat_id, QUESTION_FLOW)
        await show_today(message, db_pool, deps, notice="Хорошо, без уточнения.", edit=True)
        return
    if action in {"ok", "no"} and len(parts) > 2 and parts[2].isdigit():
        pending_id = int(parts[2])
        if action == "ok":
            result = await confirm_native_action(pending_action_id=pending_id, deps=deps, db_pool=db_pool, chat_id=chat_id)
            notice = "✓ Добавлено в календарь" if result.get("ok") else "Не удалось добавить в календарь."
        else:
            await cancel_native_action(pending_action_id=pending_id, db_pool=db_pool, chat_id=chat_id)
            notice = "Не добавлено."
        await show_today(message, db_pool, deps, notice=notice, edit=True)
        return
    await show_today(message, db_pool, deps, edit=True)


# ── screens and task card ───────────────────────────────────────────────────

async def _render_card(message: Message, db_pool: asyncpg.Pool, deps: AppDeps, task_id: int, *, notice: str | None = None) -> None:
    tz = ZoneInfo(resolve_tz_name(deps.tz_name))
    today = local_today(deps.tz_name)
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT t.id, t.title, t.kind, t.status, t.deadline, t.planned_for, t.plan_dismissed_for,
                   p.code AS project, COALESCE(tm.name, '') AS assignee
            FROM tasks t JOIN projects p ON p.id=t.project_id
            LEFT JOIN team tm ON tm.id=t.assignee_id
            WHERE t.id=$1
            """,
            int(task_id),
        )
    if not row or row["status"] in ("done", "postponed"):
        await show_today(message, db_pool, deps, notice="Эта задача уже закрыта.", edit=True)
        return

    from bot.services.day_plan import in_today_plan

    planned = in_today_plan(dict(row), today=today, tz=tz)
    meta: list[str] = []
    if row["kind"] == "personal":
        meta.append("Личное")
    elif str(row["project"] or "").upper() != "INBOX":
        meta.append(str(row["project"]))
    else:
        meta.append("Входящие")
    if row["deadline"] is not None:
        deadline = row["deadline"]
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        meta.append(f"срок {deadline.astimezone(tz):%d.%m %H:%M}")
    if row["assignee"]:
        meta.append(str(row["assignee"]))
    if planned:
        meta.append("на сегодня")

    lines = ([notice, ""] if notice else []) + [f"<b>{h(row['title'])}</b>", f"<i>{h(' · '.join(meta))}</i>"]
    tid = int(row["id"])
    kb = [
        [
            InlineKeyboardButton(text="✓ Готово", callback_data=f"tc:{tid}:done"),
            InlineKeyboardButton(
                text="Убрать из плана" if planned else "На сегодня",
                callback_data=f"tc:{tid}:{'unplan' if planned else 'plan'}",
            ),
        ],
        [
            InlineKeyboardButton(text="Срок: сегодня", callback_data=f"tc:{tid}:due:today"),
            InlineKeyboardButton(text="завтра", callback_data=f"tc:{tid}:due:tomorrow"),
        ],
        [
            InlineKeyboardButton(text="через неделю", callback_data=f"tc:{tid}:due:week"),
            InlineKeyboardButton(text="без срока", callback_data=f"tc:{tid}:due:none"),
        ],
        [InlineKeyboardButton(text="⬅ Сегодня", callback_data="nav:today")],
    ]
    await ui_render(
        bot=message.bot,
        db_pool=db_pool,
        chat_id=int(message.chat.id),
        text="\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
        screen="task_card",
        payload={"task_id": tid},
        fallback_message=message,
        preferred_message_id=int(message.message_id),
        parse_mode="HTML",
    )


async def _all_tasks_page(db_pool: asyncpg.Pool, chat_id: int) -> tuple[int, bool]:
    async with db_pool.acquire() as conn:
        payload = _ui_payload_get(await ui_get_state(conn, chat_id))
    try:
        page = max(0, int(payload.get("page") or 0))
    except (TypeError, ValueError):
        page = 0
    return page, bool(payload.get("quick_done"))


async def cb_task(callback: CallbackQuery, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    """`task:{id}` opens the card; `task:{id}:done_quick` completes from the list."""
    parts = (callback.data or "").split(":")
    message = callback.message
    if len(parts) < 2 or not parts[1].isdigit():
        await callback.answer()
        return
    task_id = int(parts[1])
    if len(parts) > 2 and parts[2] == "done_quick":
        try:
            async with db_pool.acquire() as conn:
                async with conn.transaction():
                    title = await complete_task(conn, task_id=task_id)
            await callback.answer(f"Готово: {title}"[:190])
        except TaskActionError:
            await callback.answer("Задача уже закрыта")
        page, quick_done = await _all_tasks_page(db_pool, int(message.chat.id))
        await simple_daily.ui_render_all_tasks(
            message, db_pool, tz_name=deps.tz_name, page=page, quick_done=quick_done,
            preferred_message_id=int(message.message_id),
        )
        return
    await callback.answer()
    await _render_card(message, db_pool, deps, task_id)


async def cb_task_card(callback: CallbackQuery, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    parts = (callback.data or "").split(":")
    message = callback.message
    if len(parts) < 3 or not parts[1].isdigit():
        await callback.answer()
        return
    task_id, action = int(parts[1]), parts[2]
    try:
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                if action == "done":
                    title = await complete_task(conn, task_id=task_id)
                elif action in {"plan", "unplan"}:
                    await set_planned(conn, task_id=task_id, planned=action == "plan", today=local_today(deps.tz_name))
                elif action == "due" and len(parts) > 3 and parts[3] in DUE_CHOICES:
                    await set_due(
                        conn, task_id=task_id, choice=parts[3], tz_name=deps.tz_name,
                        store_tz=bool(getattr(deps, "db_tasks_deadline_timestamptz", False)),
                    )
                else:
                    await callback.answer()
                    return
    except (TaskActionError, DayPlanError):
        await callback.answer("Задача уже закрыта")
        await show_today(message, db_pool, deps, edit=True)
        return

    await callback.answer()
    if action == "done":
        await show_today(message, db_pool, deps, notice=f"✓ Готово: {h(title)}", edit=True)
        return
    notice = {"plan": "Добавлено на сегодня", "unplan": "Убрано из плана"}.get(action, "Срок изменён")
    await _render_card(message, db_pool, deps, task_id, notice=notice)


async def cb_nav(callback: CallbackQuery, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    data = callback.data or ""
    message = callback.message
    await callback.answer()
    edit = int(message.message_id)
    if data.startswith("nav:all"):
        parts = data.split(":")
        page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
        quick_done = data.endswith(":qd1")
        await simple_daily.ui_render_all_tasks(
            message, db_pool, tz_name=deps.tz_name, page=page, quick_done=quick_done, preferred_message_id=edit,
        )
    elif data.startswith("nav:ideas"):
        parts = data.split(":")
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        await simple_daily.ui_render_ideas(message, db_pool, page=page, preferred_message_id=edit)
    elif data == "nav:secondary":
        await simple_daily.ui_render_home_more(message, db_pool, preferred_message_id=edit)
    elif data == "nav:help":
        await simple_daily.ui_render_help(message, db_pool, preferred_message_id=edit)
    else:
        # nav:home, nav:today and any screen retired from older versions.
        await show_today(message, db_pool, deps, edit=True)


async def cb_stale(callback: CallbackQuery, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    """Buttons from screens of older versions open today's screen instead of failing."""
    await callback.answer("Этот экран устарел")
    await show_today(callback.message, db_pool, deps, edit=True)


async def cmd_start(message: Message, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    await try_delete_user_message(message)
    await simple_daily.ui_render_today(
        message, db_pool, tz_name=deps.tz_name, icloud=deps.icloud, force_new=True,
    )


async def cmd_help(message: Message, db_pool: asyncpg.Pool, deps: AppDeps) -> None:
    await try_delete_user_message(message)
    await simple_daily.ui_render_help(message, db_pool)


def register(dp: Dispatcher) -> None:
    """Order matters: specific callbacks first, the catch-all for old buttons last."""
    from bot.handlers.day_plan import register as register_day_plan
    from bot.handlers.ideas import register as register_ideas
    from bot.handlers.reminders import register as register_reminders

    dp.message.register(cmd_start, CommandStart())
    dp.message.register(cmd_start, Command("menu"))
    dp.message.register(cmd_help, Command("help"))

    dp.callback_query.register(cb_capture, F.data.startswith("cap:"))
    dp.callback_query.register(cb_task_card, F.data.startswith("tc:"))
    dp.callback_query.register(cb_task, F.data.regexp(r"^task:\d+(?::done_quick)?$"))
    dp.callback_query.register(cb_nav, F.data.startswith("nav:"))
    register_day_plan(dp)
    register_ideas(dp)
    register_reminders(dp)
    dp.callback_query.register(cb_stale)

    dp.message.register(on_voice, F.voice | F.audio)
    dp.message.register(on_text, F.text)
