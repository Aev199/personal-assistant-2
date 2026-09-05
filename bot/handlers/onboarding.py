"""Initial assistant population from one unstructured text or voice dump."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import asyncpg
from aiogram import Dispatcher, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.db import db_add_event, ensure_inbox_project_id
from bot.db.runtime_state import create_pending_action, mark_pending_action_status
from bot.deps import AppDeps
from bot.fsm import InitialSetup
from bot.services.freeform_intake import (
    IntakeIntent,
    ProjectOption,
    _event_summary,
    _load_freeform_context,
    _normalize_batch_payloads,
    _parse_local_dt,
    _voice_file_meta,
)
from bot.services.onboarding_state import mark_onboarding_complete
from bot.services.pending_actions import execute_pending_action
from bot.tz import resolve_tz_name
from bot.ui import ui_render, ui_render_home
from bot.ui.state import _ui_payload_get, ui_get_state, ui_payload_with_toast, ui_set_state
from bot.utils import h, try_delete_user_message
from bot.utils.text import canon


MAX_DUMP_CHARS = 24000
MAX_ITEMS = 50


def _clean(value: object) -> str:
    return str(value or "").strip()


def _project_key(value: object) -> str:
    return canon(_clean(value))


def _existing_project(intent: IntakeIntent, projects: list[ProjectOption]) -> ProjectOption | None:
    code = _project_key(intent.project_code)
    name = _project_key(intent.project_name)
    for project in projects:
        if code and code in {_project_key(project.code), _project_key(project.name)}:
            return project
        if name and name in {_project_key(project.name), _project_key(project.code)}:
            return project
    return None


def _suggested_project_names(intents: list[IntakeIntent], projects: list[ProjectOption]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for intent in intents:
        if intent.action not in {"task", "event"}:
            continue
        if intent.action == "event" and intent.calendar_kind != "work":
            continue
        if _existing_project(intent, projects) is not None:
            continue
        name = _clean(intent.project_name)
        key = _project_key(name)
        if name and key and key not in seen:
            seen.add(key)
            names.append(name[:100])
    return names[:8]


def _intent_label(intent: IntakeIntent) -> str:
    if intent.action == "idea":
        return intent.idea_text or intent.title
    if intent.action == "reminder":
        return intent.reminder_text or intent.title
    return intent.title


def _serialize_intents(intents: list[IntakeIntent]) -> list[dict[str, Any]]:
    return [asdict(item) for item in intents[:MAX_ITEMS]]


def _deserialize_intents(items: list[dict[str, Any]]) -> list[IntakeIntent]:
    result: list[IntakeIntent] = []
    for raw in items[:MAX_ITEMS]:
        if not isinstance(raw, dict):
            continue
        allowed = {field for field in IntakeIntent.__dataclass_fields__}
        payload = {key: value for key, value in raw.items() if key in allowed}
        if isinstance(payload.get("missing_fields"), list):
            payload["missing_fields"] = tuple(payload["missing_fields"])
        try:
            result.append(IntakeIntent(**payload))
        except Exception:
            continue
    return result


def _classification_prompt(*, now_local: datetime, projects: list[ProjectOption]) -> str:
    project_lines = "\n".join(
        f"- {project.code}: {project.name}" for project in projects[:80]
    ) or "- INBOX: Входящие"
    return (
        "You convert an unstructured brain dump into a starter personal-assistant system. "
        "Return JSON only with keys actions and reply. actions is an array of independent items.\n"
        f"Local time: {now_local.strftime('%Y-%m-%d %H:%M %Z')}.\n"
        "Each action must contain action and title. Allowed action values: task, personal_task, "
        "reminder, event, idea. Split combined sentences into separate items, maximum 50.\n"
        "Use task for work obligations. Use personal_task for home, purchases, health and personal errands. "
        "Use idea only for thoughts that are not yet obligations. Use reminder only when the user explicitly "
        "asks to be reminded and gives a usable future time. Use event only for an appointment, meeting, call "
        "or time block with a usable future start; use duration_min=60 when duration is omitted.\n"
        "Never invent a date or time. Missing dates must be null. For reminder set reminder_text and "
        "remind_at_local in YYYY-MM-DD HH:MM. For event set calendar_kind, start_at_local in YYYY-MM-DD HH:MM, "
        "and duration_min. For ideas set idea_text.\n"
        "Group repeated work topics by putting the same natural project_name on related actions. Set project_code "
        "only when it exactly matches an available project below. A new natural grouping may use project_name with "
        "project_code=null. If unsure, leave both project fields null so the item goes to Inbox.\n"
        "Do not ask follow-up questions. Preserve every actionable item; prefer a task with no date over dropping it.\n"
        "AVAILABLE_PROJECTS:\n"
        f"{project_lines}\n"
        "reply must be a short Russian summary."
    )


async def _render_start(message: Message, db_pool: asyncpg.Pool) -> int:
    return await ui_render(
        bot=message.bot,
        db_pool=db_pool,
        chat_id=int(message.chat.id),
        text=(
            "<b>Добавить несколько дел</b>\n\n"
            "Пришлите список текстом или голосом. Можно начать с двух-трёх дел.\n\n"
            "Например: проверить расчёт; до пятницы отправить письмо; "
            "напомнить завтра в 10 позвонить.\n\n"
            "Перед сохранением появится общий список для проверки."
        ),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✖️ Отмена", callback_data="onboard:cancel")]
            ]
        ),
        screen="onboarding",
        payload={"step": "awaiting_dump"},
        fallback_message=message,
        parse_mode="HTML",
    )


async def _render_preview(
    message: Message,
    db_pool: asyncpg.Pool,
    *,
    intents: list[IntakeIntent],
    projects: list[ProjectOption],
    provider: str | None,
) -> int:
    counts = Counter(item.action for item in intents)
    suggested = _suggested_project_names(intents, projects)
    no_date = sum(
        1
        for item in intents
        if item.action in {"task", "personal_task", "idea"} and not item.deadline_local
    )
    inbox_count = sum(
        1
        for item in intents
        if item.action == "task" and _existing_project(item, projects) is None and not item.project_name
    )

    lines = [
        "<b>Проверьте записи</b>",
        "",
        f"Всего записей: <b>{len(intents)}</b>",
        f"💼 Работа: <b>{counts['task']}</b> · 🏡 Личное: <b>{counts['personal_task']}</b>",
        f"💡 Идеи: <b>{counts['idea']}</b> · ⏰ Напоминания: <b>{counts['reminder']}</b>",
        f"📅 События: <b>{counts['event']}</b> · Без срока: <b>{no_date}</b>",
    ]
    if suggested:
        lines.extend(["", "<b>Предлагаемые проекты</b>"])
        lines.extend(f"• {h(name)}" for name in suggested)
    if inbox_count:
        lines.append(f"\nБез проекта: <b>{inbox_count}</b>")

    sample = [_intent_label(item) for item in intents[:5] if _intent_label(item)]
    if sample:
        lines.extend(["", "<b>Первые записи</b>"])
        lines.extend(f"• {h(text[:100])}" for text in sample)
    if len(intents) > len(sample):
        lines.append(f"<i>…и ещё {len(intents) - len(sample)}</i>")

    keyboard = [
        [InlineKeyboardButton(text="✅ Сохранить всё", callback_data="onboard:confirm:projects")],
        [InlineKeyboardButton(text="📥 Без новых проектов", callback_data="onboard:confirm:inbox")],
        [
            InlineKeyboardButton(text="➕ Добавить ещё", callback_data="onboard:add"),
            InlineKeyboardButton(text="✖️ Отмена", callback_data="onboard:cancel"),
        ],
    ]
    return await ui_render(
        bot=message.bot,
        db_pool=db_pool,
        chat_id=int(message.chat.id),
        text="\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
        screen="onboarding",
        payload={"step": "preview", "items": len(intents)},
        fallback_message=message,
        parse_mode="HTML",
    )


async def _transcribe(message: Message, deps: AppDeps) -> str:
    file_id, filename, mime_type, file_size = _voice_file_meta(message)
    if not file_id:
        return ""
    max_bytes = int(os.getenv("LLM_VOICE_MAX_BYTES", str(8 * 1024 * 1024)))
    if file_size and file_size > max_bytes:
        raise RuntimeError("Голосовое слишком большое. Отправьте короче или текстом.")
    llm = deps.llm
    if llm is None or not llm.transcription_enabled:
        raise RuntimeError("Для голосового наполнения нужен работающий Gemini API.")
    progress = await message.answer("🎙 Распознаю и разбираю…")
    try:
        buffer = io.BytesIO()
        await message.bot.download(file_id, destination=buffer)
        return _clean(
            await llm.transcribe_audio(
                audio_bytes=buffer.getvalue(),
                filename=filename,
                mime_type=mime_type,
            )
        )
    finally:
        try:
            await message.bot.delete_message(message.chat.id, progress.message_id)
        except Exception:
            pass


async def cb_onboarding_start(
    callback: CallbackQuery,
    state: FSMContext,
    db_pool: asyncpg.Pool,
    deps: AppDeps,
) -> None:
    if deps.admin_id and (not callback.from_user or callback.from_user.id != deps.admin_id):
        return await callback.answer("Недоступно", show_alert=True)
    await callback.answer()
    await state.clear()
    await state.set_state(InitialSetup.awaiting_dump)
    await state.update_data(onboarding_source_text="", onboarding_append=False)
    await _render_start(callback.message, db_pool)


async def cmd_onboarding_start(
    message: Message,
    state: FSMContext,
    db_pool: asyncpg.Pool,
    deps: AppDeps,
) -> None:
    if deps.admin_id and (not message.from_user or message.from_user.id != deps.admin_id):
        return
    await state.clear()
    await state.set_state(InitialSetup.awaiting_dump)
    await state.update_data(onboarding_source_text="", onboarding_append=False)
    await try_delete_user_message(message)
    await _render_start(message, db_pool)


async def msg_onboarding_dump(
    message: Message,
    state: FSMContext,
    db_pool: asyncpg.Pool,
    deps: AppDeps,
) -> None:
    if deps.admin_id and (not message.from_user or message.from_user.id != deps.admin_id):
        return
    llm = deps.llm
    if llm is None or not llm.enabled:
        return await ui_render(
            bot=message.bot,
            db_pool=db_pool,
            chat_id=int(message.chat.id),
            text="⚠️ Не настроен ни Gemini, ни DeepSeek. Добавьте API-ключ и повторите.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home")]]
            ),
            screen="onboarding",
            payload={"step": "error"},
            fallback_message=message,
            parse_mode="HTML",
        )

    try:
        incoming = _clean(message.text) if message.text else await _transcribe(message, deps)
        await try_delete_user_message(message)
        if not incoming:
            raise RuntimeError("Не удалось получить текст.")
        data = await state.get_data()
        previous = _clean(data.get("onboarding_source_text"))
        combined = "\n".join(part for part in (previous, incoming) if part)
        if len(combined) > MAX_DUMP_CHARS:
            combined = combined[:MAX_DUMP_CHARS]

        tz_name = resolve_tz_name(deps.tz_name)
        async with db_pool.acquire() as conn:
            _, _, projects, _ = await _load_freeform_context(
                conn,
                chat_id=int(message.chat.id),
            )
        response = await llm.classify_intake_batch(
            system_prompt=_classification_prompt(
                now_local=datetime.now(ZoneInfo(tz_name)),
                projects=projects,
            ),
            user_prompt=combined,
        )
        intents, _ = _normalize_batch_payloads(response)
        intents = [item for item in intents if not item.needs_followup][:MAX_ITEMS]
        if not intents:
            raise RuntimeError("Не удалось выделить отдельные записи. Добавьте больше конкретных дел.")

        await state.update_data(
            onboarding_source_text=combined,
            onboarding_intents=_serialize_intents(intents),
            onboarding_provider=llm.last_provider,
            onboarding_append=False,
        )
        await _render_preview(
            message,
            db_pool,
            intents=intents,
            projects=projects,
            provider=llm.last_provider,
        )
    except Exception as exc:
        await ui_render(
            bot=message.bot,
            db_pool=db_pool,
            chat_id=int(message.chat.id),
            text=f"⚠️ <b>Не удалось разобрать список.</b>\n\n{h(str(exc))}\n\nПопробуйте ещё раз текстом.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🔄 Попробовать снова", callback_data="onboard:start")],
                    [InlineKeyboardButton(text="✖️ Отмена", callback_data="onboard:cancel")],
                ]
            ),
            screen="onboarding",
            payload={"step": "error"},
            fallback_message=message,
            parse_mode="HTML",
        )


_TRANSLIT = str.maketrans(
    {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
        "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
        "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
        "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch",
        "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    }
)


def _base_project_code(name: str) -> str:
    latin = _clean(name).lower().translate(_TRANSLIT)
    words = re.findall(r"[a-z0-9]+", latin)
    if not words:
        return "PROJECT"
    if len(words) >= 2:
        code = "".join(word[0] for word in words[:4]).upper()
    else:
        code = words[0][:8].upper()
    return code or "PROJECT"


async def _create_project(conn: asyncpg.Connection, name: str) -> ProjectOption:
    existing = await conn.fetchrow(
        "SELECT id, code, name FROM projects WHERE lower(name)=lower($1) LIMIT 1",
        name,
    )
    if existing:
        return ProjectOption(int(existing["id"]), str(existing["code"]), str(existing["name"]))
    base = _base_project_code(name)
    code = base
    suffix = 2
    while await conn.fetchval("SELECT 1 FROM projects WHERE code=$1", code):
        code = f"{base[:9]}-{suffix}"
        suffix += 1
    project_id = int(
        await conn.fetchval(
            "INSERT INTO projects(code, name, status) VALUES($1, $2, 'active') RETURNING id",
            code,
            name,
        )
    )
    await db_add_event(conn, "project_created", project_id, None, f"🆕 Onboarding: [{code}] {name}")
    return ProjectOption(project_id, code, name)


def _fingerprint(kind: str, payload: dict[str, Any]) -> str:
    raw = json.dumps({"kind": kind, "payload": payload}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _execute_import_item(
    *,
    message: Message,
    db_pool: asyncpg.Pool,
    deps: AppDeps,
    kind: str,
    payload: dict[str, Any],
    summary: str,
) -> None:
    fingerprint = _fingerprint(kind, payload)
    async with db_pool.acquire() as conn:
        pending_id = await create_pending_action(
            conn,
            chat_id=int(message.chat.id),
            kind=kind,
            payload={**payload, "source": "onboarding"},
            source_message_id=int(message.message_id),
            fingerprint=fingerprint,
            ttl_sec=3600,
        )
    pending = {
        "id": int(pending_id),
        "kind": kind,
        "payload": {**payload, "source": "onboarding"},
        "fingerprint": fingerprint,
    }
    try:
        await execute_pending_action(
            pending,
            db_pool=db_pool,
            deps=deps,
            chat_id=int(message.chat.id),
        )
    except Exception as exc:
        async with db_pool.acquire() as conn:
            await mark_pending_action_status(
                conn,
                pending_action_id=int(pending_id),
                status="failed",
                last_error=str(exc),
            )
        raise RuntimeError(f"{summary}: {exc}") from exc


async def _save_onboarding(
    message: Message,
    *,
    state: FSMContext,
    db_pool: asyncpg.Pool,
    deps: AppDeps,
    create_projects: bool,
) -> tuple[int, list[str], int]:
    data = await state.get_data()
    intents = _deserialize_intents(list(data.get("onboarding_intents") or []))
    if not intents:
        raise RuntimeError("Черновик наполнения потерян. Запустите мастер заново.")

    tz_name = resolve_tz_name(deps.tz_name)
    tz = ZoneInfo(tz_name)
    async with db_pool.acquire() as conn:
        _, _, existing_projects, _ = await _load_freeform_context(
            conn,
            chat_id=int(message.chat.id),
        )
        inbox_id = await ensure_inbox_project_id(conn)
        project_map: dict[str, ProjectOption] = {
            _project_key(project.code): project for project in existing_projects
        }
        project_map.update({_project_key(project.name): project for project in existing_projects})
        created_projects = 0
        if create_projects:
            for name in _suggested_project_names(intents, existing_projects):
                project = await _create_project(conn, name)
                project_map[_project_key(project.name)] = project
                project_map[_project_key(project.code)] = project
                if all(project.id != old.id for old in existing_projects):
                    created_projects += 1

        inbox_row = await conn.fetchrow("SELECT id, code, name FROM projects WHERE id=$1", int(inbox_id))
        inbox = ProjectOption(int(inbox_row["id"]), str(inbox_row["code"]), str(inbox_row["name"]))

    saved = 0
    errors: list[str] = []
    gtasks_ready = bool(deps.gtasks is not None and deps.gtasks.enabled())
    icloud_ready = bool(
        os.getenv("ICLOUD_APPLE_ID", "").strip()
        and os.getenv("ICLOUD_APP_PASSWORD", "").strip()
    )

    for intent in intents:
        try:
            project = None
            if create_projects:
                project = project_map.get(_project_key(intent.project_code)) or project_map.get(
                    _project_key(intent.project_name)
                )
            if project is None and create_projects:
                project = _existing_project(intent, existing_projects)
            project = project or inbox

            if intent.action == "task":
                deadline = _parse_local_dt(intent.deadline_local, tz_name) if intent.deadline_local else None
                await _execute_import_item(
                    message=message,
                    db_pool=db_pool,
                    deps=deps,
                    kind="task",
                    payload={
                        "title": intent.title,
                        "project_id": int(project.id),
                        "project_code": project.code,
                        "assignee_id": None,
                        "assignee_name": None,
                        "deadline_local": deadline.isoformat() if deadline else "",
                    },
                    summary=intent.title,
                )
            elif intent.action == "personal_task" and gtasks_ready:
                due = _parse_local_dt(intent.deadline_local, tz_name) if intent.deadline_local else None
                await _execute_import_item(
                    message=message,
                    db_pool=db_pool,
                    deps=deps,
                    kind="personal_task",
                    payload={"title": intent.title, "deadline_local": due.isoformat() if due else ""},
                    summary=intent.title,
                )
            elif intent.action == "idea" and gtasks_ready:
                idea = intent.idea_text or intent.title
                await _execute_import_item(
                    message=message,
                    db_pool=db_pool,
                    deps=deps,
                    kind="idea",
                    payload={"idea_text": idea},
                    summary=idea,
                )
            elif intent.action == "reminder":
                remind_at = _parse_local_dt(intent.remind_at_local, tz_name)
                if remind_at is not None and remind_at > datetime.now(tz):
                    await _execute_import_item(
                        message=message,
                        db_pool=db_pool,
                        deps=deps,
                        kind="reminder",
                        payload={
                            "reminder_text": intent.reminder_text or intent.title,
                            "remind_at_local": remind_at.isoformat(),
                        },
                        summary=intent.reminder_text or intent.title,
                    )
                else:
                    raise RuntimeError("время напоминания не определено или уже прошло")
            elif intent.action == "event":
                start = _parse_local_dt(intent.start_at_local, tz_name)
                calendar_kind = intent.calendar_kind or "personal"
                calendar_url = os.getenv(
                    "ICLOUD_CALENDAR_URL_WORK" if calendar_kind == "work" else "ICLOUD_CALENDAR_URL_PERSONAL",
                    "",
                ).strip()
                if icloud_ready and start and start > datetime.now(tz) and calendar_url:
                    summary = _event_summary(calendar_kind, intent.title, project.code if calendar_kind == "work" else None)
                    await _execute_import_item(
                        message=message,
                        db_pool=db_pool,
                        deps=deps,
                        kind="event",
                        payload={
                            "title": intent.title,
                            "calendar_kind": calendar_kind,
                            "calendar_url": calendar_url,
                            "summary": summary,
                            "start_local": start.isoformat(),
                            "duration_min": int(intent.duration_min or 60),
                            "project_id": int(project.id) if calendar_kind == "work" else None,
                            "project_code": project.code if calendar_kind == "work" else None,
                        },
                        summary=summary,
                    )
                else:
                    deadline = start
                    await _execute_import_item(
                        message=message,
                        db_pool=db_pool,
                        deps=deps,
                        kind="task",
                        payload={
                            "title": intent.title,
                            "project_id": int(project.id),
                            "project_code": project.code,
                            "assignee_id": None,
                            "assignee_name": None,
                            "deadline_local": deadline.isoformat() if deadline else "",
                        },
                        summary=intent.title,
                    )
            else:
                fallback_title = _intent_label(intent) or "Неопознанная запись"
                prefix = "Идея: " if intent.action == "idea" else ""
                await _execute_import_item(
                    message=message,
                    db_pool=db_pool,
                    deps=deps,
                    kind="task",
                    payload={
                        "title": prefix + fallback_title,
                        "project_id": int(inbox.id),
                        "project_code": inbox.code,
                        "assignee_id": None,
                        "assignee_name": None,
                        "deadline_local": "",
                    },
                    summary=fallback_title,
                )
            saved += 1
        except Exception as exc:
            errors.append(str(exc)[:180])

    if saved:
        async with db_pool.acquire() as conn:
            await mark_onboarding_complete(conn, int(message.chat.id))
    return saved, errors, created_projects


async def cb_onboarding_action(
    callback: CallbackQuery,
    state: FSMContext,
    db_pool: asyncpg.Pool,
    deps: AppDeps,
) -> None:
    if deps.admin_id and (not callback.from_user or callback.from_user.id != deps.admin_id):
        return await callback.answer("Недоступно", show_alert=True)
    await callback.answer()
    action = (callback.data or "").split(":")
    command = action[1] if len(action) > 1 else ""

    if command == "cancel":
        await state.clear()
        return await ui_render_home(callback.message, db_pool, tz_name=deps.tz_name)
    if command == "start":
        return await cb_onboarding_start(callback, state, db_pool, deps)
    if command == "add":
        data = await state.get_data()
        previous = _clean(data.get("onboarding_source_text"))
        await state.set_state(InitialSetup.awaiting_dump)
        await state.update_data(onboarding_source_text=previous, onboarding_append=True)
        return await ui_render(
            bot=callback.bot,
            db_pool=db_pool,
            chat_id=int(callback.message.chat.id),
            text=(
                "➕ <b>Добавьте ещё</b>\n\n"
                "Пришлите следующий текст или голосовое. Я объединю его с предыдущим списком "
                "и пересоберу общий итог."
            ),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data="onboard:cancel")]]
            ),
            screen="onboarding",
            payload={"step": "awaiting_more"},
            fallback_message=callback.message,
            parse_mode="HTML",
        )
    if command != "confirm":
        return

    mode = action[2] if len(action) > 2 else "projects"
    try:
        saved, errors, created_projects = await _save_onboarding(
            callback.message,
            state=state,
            db_pool=db_pool,
            deps=deps,
            create_projects=(mode == "projects"),
        )
        await state.clear()
        toast = f"✅ Сохранено записей: {saved}."
        if created_projects:
            toast += f" Создано проектов: {created_projects}."
        if errors:
            toast += f" Не удалось: {len(errors)}."
        async with db_pool.acquire() as conn:
            ui_state = await ui_get_state(conn, int(callback.message.chat.id))
            payload = _ui_payload_get(ui_state)
            payload = ui_payload_with_toast(payload, toast, ttl_sec=35)
            await ui_set_state(conn, int(callback.message.chat.id), ui_payload=payload)
        await ui_render_home(callback.message, db_pool, tz_name=deps.tz_name)
    except Exception as exc:
        await ui_render(
            bot=callback.bot,
            db_pool=db_pool,
            chat_id=int(callback.message.chat.id),
            text=f"❌ <b>Не удалось сохранить систему.</b>\n\n{h(str(exc))}",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🔄 Начать заново", callback_data="onboard:start")],
                    [InlineKeyboardButton(text="⬅️ Домой", callback_data="nav:home")],
                ]
            ),
            screen="onboarding",
            payload={"step": "save_error"},
            fallback_message=callback.message,
            parse_mode="HTML",
        )


def register(dp: Dispatcher) -> None:
    dp.message.register(cmd_onboarding_start, Command("setup"))
    dp.callback_query.register(cb_onboarding_start, F.data == "onboard:start")
    dp.callback_query.register(cb_onboarding_action, F.data.startswith("onboard:"))
    dp.message.register(
        msg_onboarding_dump,
        StateFilter(InitialSetup.awaiting_dump),
        lambda message: bool(message.text or message.voice or message.audio),
    )
