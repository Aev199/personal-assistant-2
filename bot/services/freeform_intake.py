"""LLM-based free-form intake for text and voice messages."""

from __future__ import annotations

import io
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import asyncpg
from aiogram.types import Message

from bot.db import db_add_event, ensure_inbox_project_id, get_current_project_id
from bot.deps import AppDeps
from bot.services.background import fire_and_forget
from bot.services.vault_sync import background_project_sync
from bot.tz import fmt_local, resolve_tz_name, to_db_utc
from bot.ui.screens import (
    ui_render_add_menu,
    ui_render_all_tasks,
    ui_render_help,
    ui_render_home,
    ui_render_inbox,
    ui_render_overdue,
    ui_render_projects_portfolio,
    ui_render_stats,
    ui_render_team,
    ui_render_today,
    ui_render_work,
)
from bot.ui.state import _ui_payload_get, ui_get_state, ui_payload_with_toast, ui_set_state
from bot.utils.datetime import parse_datetime_ru


logger = logging.getLogger(__name__)

SUPPORTED_SCREENS = {
    "home",
    "projects",
    "today",
    "overdue",
    "all_tasks",
    "work",
    "inbox",
    "help",
    "add",
    "team",
    "stats",
}


@dataclass
class IntakeIntent:
    action: str
    title: str = ""
    deadline_local: str | None = None
    reminder_text: str = ""
    remind_at_local: str | None = None
    project_code: str | None = None
    screen: str | None = None
    reply: str = ""


def _clean(s: object) -> str:
    return str(s or "").strip()


def _normalize_screen(value: object) -> str | None:
    screen = _clean(value).lower()
    aliases = {
        "all": "all_tasks",
        "all_tasks": "all_tasks",
        "projects": "projects",
        "today": "today",
        "overdue": "overdue",
        "work": "work",
        "inbox": "inbox",
        "help": "help",
        "home": "home",
        "add": "add",
        "team": "team",
        "stats": "stats",
    }
    norm = aliases.get(screen)
    return norm if norm in SUPPORTED_SCREENS else None


def _normalize_intake_payload(payload: object) -> IntakeIntent:
    data = payload if isinstance(payload, dict) else {}
    action = _clean(data.get("action")).lower()
    if action not in {"task", "reminder", "nav", "reply"}:
        action = "reply"

    title = _clean(data.get("title"))
    reminder_text = _clean(data.get("reminder_text") or data.get("text"))
    reply = _clean(data.get("reply"))
    project_code = _clean(data.get("project_code")).upper() or None
    screen = _normalize_screen(data.get("screen"))

    if action == "task" and not title:
        action = "reply"
        reply = reply or "Не смог собрать задачу из сообщения."
    if action == "reminder" and (not reminder_text or not _clean(data.get("remind_at_local"))):
        action = "reply"
        reply = reply or "Не смог собрать напоминание из сообщения."
    if action == "nav" and not screen:
        action = "reply"
        reply = reply or "Не понял, какой экран нужно открыть."

    return IntakeIntent(
        action=action,
        title=title,
        deadline_local=_clean(data.get("deadline_local")) or None,
        reminder_text=reminder_text,
        remind_at_local=_clean(data.get("remind_at_local")) or None,
        project_code=project_code,
        screen=screen,
        reply=reply,
    )


def _parse_local_dt(value: str | None, tz_name: str) -> datetime | None:
    raw = _clean(value)
    if not raw:
        return None
    tz = ZoneInfo(tz_name)
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=tz)
        except Exception:
            pass
    return parse_datetime_ru(raw, tz_name, prefer_future=True)


async def _render_screen(
    message: Message,
    db_pool: asyncpg.Pool,
    deps: AppDeps,
    *,
    screen: str,
    payload: dict | None = None,
) -> int:
    tz_name = resolve_tz_name(deps.tz_name)
    if screen == "home":
        return await ui_render_home(message, db_pool, tz_name=tz_name, force_new=False)
    if screen == "projects":
        return await ui_render_projects_portfolio(message, db_pool, tz_name=tz_name, force_new=False)
    if screen == "today":
        return await ui_render_today(message, db_pool, tz_name=tz_name, force_new=False)
    if screen == "overdue":
        page = int((payload or {}).get("page") or 0)
        return await ui_render_overdue(message, db_pool, page=page, tz_name=tz_name, force_new=False)
    if screen == "all_tasks":
        page = int((payload or {}).get("page") or 0)
        filter_key = _clean((payload or {}).get("filter") or "all").lower() or "all"
        return await ui_render_all_tasks(message, db_pool, tz_name=tz_name, page=page, filter_key=filter_key, force_new=False)
    if screen == "work":
        page = int((payload or {}).get("page") or 0)
        return await ui_render_work(message, db_pool, tz_name=tz_name, page=page, force_new=False)
    if screen == "inbox":
        page = int((payload or {}).get("page") or 0)
        return await ui_render_inbox(message, db_pool, tz_name=tz_name, page=page, force_new=False)
    if screen == "help":
        return await ui_render_help(message, db_pool, tz_name=tz_name, force_new=False)
    if screen == "add":
        return await ui_render_add_menu(message, db_pool, tz_name=tz_name, force_new=False)
    if screen == "team":
        return await ui_render_team(message, db_pool, tz_name=tz_name, force_new=False)
    if screen == "stats":
        return await ui_render_stats(message, db_pool, tz_name=tz_name, force_new=False)
    return await ui_render_home(message, db_pool, tz_name=tz_name, force_new=False)


async def _rerender_with_toast(message: Message, db_pool: asyncpg.Pool, deps: AppDeps, toast: str) -> int:
    async with db_pool.acquire() as conn:
        ui_state = await ui_get_state(conn, int(message.chat.id))
        screen = _clean(ui_state.get("ui_screen") or "home").lower()
        payload = _ui_payload_get(ui_state)
        payload = ui_payload_with_toast(payload, toast, ttl_sec=25)
        await ui_set_state(conn, int(message.chat.id), ui_payload=payload)
    if screen not in SUPPORTED_SCREENS:
        screen = "home"
    return await _render_screen(message, db_pool, deps, screen=screen, payload=payload)


async def _resolve_project(
    conn: asyncpg.Connection,
    *,
    chat_id: int,
    requested_code: str | None,
) -> tuple[int | None, str | None, str | None]:
    code = _clean(requested_code).upper() or None
    if code:
        row = await conn.fetchrow(
            "SELECT id, code FROM projects WHERE UPPER(code)=UPPER($1) AND status='active' LIMIT 1",
            code,
        )
        if not row:
            return None, None, f"Не нашёл проект {code}."
        return int(row["id"]), str(row["code"]), None

    current_id = await get_current_project_id(conn, int(chat_id))
    if current_id:
        row = await conn.fetchrow("SELECT id, code FROM projects WHERE id=$1 AND status='active' LIMIT 1", int(current_id))
        if row:
            return int(row["id"]), str(row["code"]), None

    inbox_id = await ensure_inbox_project_id(conn)
    return int(inbox_id), "INBOX", None


def _voice_file_meta(message: Message) -> tuple[str | None, str, str | None, int]:
    if message.voice:
        return (
            message.voice.file_id,
            f"voice_{_clean(message.voice.file_unique_id) or 'note'}.ogg",
            message.voice.mime_type or "audio/ogg",
            int(message.voice.file_size or 0),
        )
    if message.audio:
        return (
            message.audio.file_id,
            message.audio.file_name or "audio.mp3",
            message.audio.mime_type or "audio/mpeg",
            int(message.audio.file_size or 0),
        )
    return None, "audio.bin", None, 0


def _intake_system_prompt(*, now_local: datetime, tz_name: str, current_project_code: str | None) -> str:
    project = current_project_code or "INBOX"
    return (
        "Ты маршрутизатор для личного ассистента задач. "
        "Верни только JSON без markdown.\n"
        f"Локальное время пользователя: {now_local.strftime('%Y-%m-%d %H:%M')} ({tz_name}). "
        f"Текущий проект: {project}.\n"
        "Разрешённые action: task, reminder, nav, reply.\n"
        "task: создать задачу. Поля: title, deadline_local (YYYY-MM-DD HH:MM или null), project_code (или null).\n"
        "reminder: создать напоминание. Поля: reminder_text, remind_at_local (YYYY-MM-DD HH:MM).\n"
        "nav: открыть экран. Поле screen: home, projects, today, overdue, all_tasks, work, inbox, help, add, team, stats.\n"
        "reply: короткий ответ, если запрос не является действием или данных не хватает.\n"
        "Если пользователь явно просит напомнить — reminder. "
        "Если просит показать/открыть экран — nav. "
        "Если формулировка похожа на действие/дело — task. "
        "reply держи короче 120 символов."
    )


async def handle_freeform_text(
    message: Message,
    *,
    deps: AppDeps,
    db_pool: asyncpg.Pool,
    raw_text: str,
    source: str = "text",
) -> bool:
    llm = getattr(deps, "llm", None)
    if llm is None or not getattr(llm, "enabled", False):
        return False

    text = _clean(raw_text)
    if not text:
        return False

    tz_name = resolve_tz_name(deps.tz_name)
    tz = ZoneInfo(tz_name)
    current_project_code: str | None = None
    async with db_pool.acquire() as conn:
        current_project_id = await get_current_project_id(conn, int(message.chat.id))
        if current_project_id:
            current_project_code = await conn.fetchval("SELECT code FROM projects WHERE id=$1", int(current_project_id))

    try:
        payload = await llm.classify_intake(
            system_prompt=_intake_system_prompt(
                now_local=datetime.now(tz),
                tz_name=tz_name,
                current_project_code=current_project_code,
            ),
            user_prompt=text,
        )
        intent = _normalize_intake_payload(payload)
    except Exception:
        logger.exception("freeform classify failed", extra={"source": source})
        return False

    if intent.action == "nav" and intent.screen:
        await _render_screen(message, db_pool, deps, screen=intent.screen, payload={})
        return True

    if intent.action == "task":
        deadline_local = _parse_local_dt(intent.deadline_local, tz_name) if intent.deadline_local else None
        project_error: str | None = None
        project_id: int | None = None
        project_code: str | None = None
        try:
            async with db_pool.acquire() as conn:
                project_id, project_code, project_error = await _resolve_project(
                    conn,
                    chat_id=int(message.chat.id),
                    requested_code=intent.project_code,
                )
                if project_error or project_id is None:
                    raise ValueError(project_error or "Не удалось определить проект для задачи.")

                task_id = await conn.fetchval(
                    "INSERT INTO tasks (project_id, title, assignee_id, deadline) VALUES ($1,$2,NULL,$3) RETURNING id",
                    int(project_id),
                    intent.title,
                    to_db_utc(
                        deadline_local,
                        tz_name=tz_name,
                        store_tz=bool(getattr(deps, "db_tasks_deadline_timestamptz", False)),
                    ) if deadline_local else None,
                )
                await db_add_event(
                    conn,
                    "task_created",
                    int(project_id),
                    int(task_id),
                        f"LLM/{source}: [{project_code}] #{int(task_id)} {intent.title}",
                )
            if project_id is not None:
                fire_and_forget(
                    background_project_sync(
                        int(project_id),
                        db_pool,
                        deps.vault,
                        error_logger=(deps.db_log_error or (lambda _w, _e, _c=None: None)),
                    ),
                    label="vault_sync",
                )
        except ValueError as e:
            await _rerender_with_toast(message, db_pool, deps, str(e))
            return True
        except Exception:
            logger.exception("freeform task create failed", extra={"source": source})
            return False

        toast = f"✅ Задача: {intent.title}"
        if deadline_local:
            toast += f" · до {fmt_local(to_db_utc(deadline_local, tz_name=tz_name, store_tz=False), tz)}"
        await _rerender_with_toast(message, db_pool, deps, toast)
        return True

    if intent.action == "reminder":
        remind_local = _parse_local_dt(intent.remind_at_local, tz_name)
        if remind_local is None:
            await _rerender_with_toast(message, db_pool, deps, "Не смог разобрать дату напоминания.")
            return True
        if remind_local <= datetime.now(tz):
            await _rerender_with_toast(message, db_pool, deps, "Время напоминания уже прошло. Укажите будущее время.")
            return True
        try:
            async with db_pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO reminders (text, remind_at, repeat) VALUES ($1,$2,'none')",
                    intent.reminder_text,
                    to_db_utc(
                        remind_local,
                        tz_name=tz_name,
                        store_tz=bool(getattr(deps, "db_reminders_remind_at_timestamptz", False)),
                    ),
                )
                await db_add_event(conn, "reminder_created", None, None, f"LLM/{source}: {intent.reminder_text}")
        except Exception:
            logger.exception("freeform reminder create failed", extra={"source": source})
            return False

        when_txt = remind_local.strftime("%d.%m %H:%M")
        await _rerender_with_toast(message, db_pool, deps, f"✅ Напоминание: {when_txt}")
        return True

    reply = intent.reply or "Не понял запрос."
    await _rerender_with_toast(message, db_pool, deps, reply)
    return True


async def handle_freeform_voice(message: Message, *, deps: AppDeps, db_pool: asyncpg.Pool) -> bool:
    llm = getattr(deps, "llm", None)
    if llm is None or not getattr(llm, "enabled", False):
        return False

    file_id, filename, mime_type, file_size = _voice_file_meta(message)
    if not file_id:
        return False

    max_bytes = int(os.getenv("LLM_VOICE_MAX_BYTES", str(8 * 1024 * 1024)))
    if file_size and file_size > max_bytes:
        await _rerender_with_toast(message, db_pool, deps, "Голосовое слишком большое. Отправьте короче или текстом.")
        return True

    try:
        buf = io.BytesIO()
        await message.bot.download(file_id, destination=buf)
        transcript = await llm.transcribe_audio(
            audio_bytes=buf.getvalue(),
            filename=filename,
            mime_type=mime_type,
        )
    except Exception:
        logger.exception("voice transcription failed")
        return False

    transcript = _clean(transcript)
    if not transcript:
        await _rerender_with_toast(message, db_pool, deps, "Не удалось распознать голосовое сообщение.")
        return True

    return await handle_freeform_text(
        message,
        deps=deps,
        db_pool=db_pool,
        raw_text=transcript,
        source="voice",
    )
