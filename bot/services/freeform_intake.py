"""LLM-based free-form intake for text and voice messages."""

from __future__ import annotations

import io
import json
import hashlib
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TypeVar
from zoneinfo import ZoneInfo

import asyncpg
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot.db import db_add_event, ensure_inbox_project_id, get_current_project_id, get_persona_mode
from bot.db.runtime_state import (
    clear_conversation_state,
    find_recent_action,
    get_conversation_state,
    set_conversation_state,
)
from bot.deps import AppDeps
from bot.fsm.states import FreeformFollowup
from bot.services.background import fire_and_forget
from bot.services.gtasks_service import due_from_local_date, get_or_create_list_id
from bot.services.pending_actions import create_pending_preview
from bot.services.vault_sync import background_project_sync
from bot.tz import fmt_local, resolve_tz_name, to_db_utc
from bot.persona import is_solo_mode
from bot.ui.screens import (
    ui_render_add_menu,
    ui_render_all_tasks,
    ui_render_help,
    ui_render_home,
    ui_render_home_more,
    ui_render_inbox,
    ui_render_overdue,
    ui_render_projects_portfolio,
    ui_render_stats,
    ui_render_team,
    ui_render_today,
    ui_render_work,
)
from bot.ui.state import _ui_payload_get, ui_get_state, ui_payload_with_toast, ui_set_state
from bot.utils.datetime import parse_datetime_ru, quick_extract_datetime_ru
from bot.utils.text import canon


logger = logging.getLogger(__name__)
T = TypeVar("T")

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
    idea_text: str = ""
    deadline_local: str | None = None
    reminder_text: str = ""
    remind_at_local: str | None = None
    calendar_kind: str | None = None
    start_at_local: str | None = None
    duration_min: int | None = None
    project_code: str | None = None
    project_name: str | None = None
    assignee_name: str | None = None
    reply: str = ""
    needs_followup: bool = False
    followup_prompt: str = ""
    followup_action: str | None = None
    missing_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProjectOption:
    id: int
    code: str
    name: str


@dataclass(frozen=True)
class TeamOption:
    id: int
    name: str


def _clean(s: object) -> str:
    return str(s or "").strip()


def _normalize_intake_payload(payload: object) -> IntakeIntent:
    data = payload if isinstance(payload, dict) else {}
    action = _clean(data.get("action")).lower()
    if action not in {"task", "personal_task", "reminder", "event", "idea", "reply"}:
        action = "reply"
    requested_action = action

    title = _clean(data.get("title"))
    idea_text = _clean(data.get("idea_text") or data.get("text") or data.get("title"))
    reminder_text = _clean(data.get("reminder_text") or data.get("text"))
    reply = _clean(data.get("reply"))
    calendar_kind = _clean(data.get("calendar_kind")).lower() or None
    if calendar_kind not in {"work", "personal"}:
        calendar_kind = None
    start_at_local = _clean(data.get("start_at_local")) or None
    try:
        duration_min = int(data.get("duration_min")) if data.get("duration_min") is not None else None
    except Exception:
        duration_min = None
    project_code = _clean(data.get("project_code")).upper() or None
    project_name = _clean(data.get("project_name")) or None
    assignee_name = _clean(data.get("assignee_name")) or None
    needs_followup = False
    followup_prompt = ""
    followup_action: str | None = None
    missing_fields: list[str] = []

    if action == "task" and not title:
        action = "reply"
        needs_followup = True
        followup_action = requested_action
        missing_fields = ["title"]
        followup_prompt = reply or "\u041d\u0435 \u0441\u043c\u043e\u0433 \u0432\u044b\u0434\u0435\u043b\u0438\u0442\u044c \u0437\u0430\u0434\u0430\u0447\u0443. \u0423\u0442\u043e\u0447\u043d\u0438\u0442\u0435, \u0447\u0442\u043e \u043d\u0443\u0436\u043d\u043e \u0441\u0434\u0435\u043b\u0430\u0442\u044c."
        reply = reply or followup_prompt
    if action == "personal_task" and not title:
        action = "reply"
        needs_followup = True
        followup_action = requested_action
        missing_fields = ["title"]
        followup_prompt = reply or "\u041d\u0435 \u0432\u0438\u0436\u0443 \u0442\u0435\u043a\u0441\u0442 \u043b\u0438\u0447\u043d\u043e\u0439 \u0437\u0430\u0434\u0430\u0447\u0438. \u0423\u0442\u043e\u0447\u043d\u0438\u0442\u0435, \u0447\u0442\u043e \u043d\u0443\u0436\u043d\u043e \u0441\u0434\u0435\u043b\u0430\u0442\u044c."
        reply = reply or followup_prompt
    if action == "reminder" and (not reminder_text or not _clean(data.get("remind_at_local"))):
        action = "reply"
        needs_followup = True
        followup_action = requested_action
        if not reminder_text:
            missing_fields.append("reminder_text")
        if not _clean(data.get("remind_at_local")):
            missing_fields.append("remind_at_local")
        followup_prompt = reply or "\u0423\u043a\u0430\u0436\u0438\u0442\u0435, \u043a\u043e\u0433\u0434\u0430 \u043d\u0430\u043f\u043e\u043c\u043d\u0438\u0442\u044c. \u041d\u0430\u043f\u0440\u0438\u043c\u0435\u0440: \u0437\u0430\u0432\u0442\u0440\u0430 \u0432 10:00."
        reply = reply or followup_prompt
    if action == "event" and (not title or not calendar_kind or not start_at_local or duration_min is None):
        action = "reply"
        needs_followup = True
        followup_action = requested_action
        if not title:
            missing_fields.append("title")
        if not calendar_kind:
            missing_fields.append("calendar_kind")
        if not start_at_local:
            missing_fields.append("start_at_local")
        if duration_min is None:
            missing_fields.append("duration_min")
        followup_prompt = reply or "\u0423\u0442\u043e\u0447\u043d\u0438\u0442\u0435 \u0441\u043e\u0431\u044b\u0442\u0438\u0435: \u043a\u0430\u043b\u0435\u043d\u0434\u0430\u0440, \u0432\u0440\u0435\u043c\u044f \u0438 \u043d\u0430\u0437\u0432\u0430\u043d\u0438\u0435."
        reply = reply or followup_prompt
    if action == "idea" and not idea_text:
        action = "reply"
        needs_followup = True
        followup_action = requested_action
        missing_fields = ["idea_text"]
        followup_prompt = reply or "\u041d\u0435 \u0432\u0438\u0436\u0443 \u0442\u0435\u043a\u0441\u0442 \u0438\u0434\u0435\u0438. \u041f\u0440\u0438\u0448\u043b\u0438\u0442\u0435 \u0435\u0451 \u043e\u0434\u043d\u043e\u0439 \u0444\u0440\u0430\u0437\u043e\u0439."
        reply = reply or followup_prompt

    return IntakeIntent(
        action=action,
        title=title,
        idea_text=idea_text,
        deadline_local=_clean(data.get("deadline_local")) or None,
        reminder_text=reminder_text,
        remind_at_local=_clean(data.get("remind_at_local")) or None,
        calendar_kind=calendar_kind,
        start_at_local=start_at_local,
        duration_min=duration_min,
        project_code=project_code,
        project_name=project_name,
        assignee_name=assignee_name,
        reply=reply,
        needs_followup=needs_followup,
        followup_prompt=followup_prompt,
        followup_action=followup_action,
        missing_fields=tuple(dict.fromkeys(field for field in missing_fields if field)),
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


def _merge_freeform_text(base_text: str | None, raw_text: str | None) -> str:
    parts = [_clean(base_text), _clean(raw_text)]
    return "\n".join(part for part in parts if part)


def _action_hint_from_text(raw_text: str) -> str | None:
    text = _clean(raw_text)
    if not text:
        return None
    lowered = canon(text)
    marker_map = {
        "идея:": "idea",
        "идея ": "idea",
        "idea:": "idea",
        "idea ": "idea",
        "личное:": "personal_task",
        "личная задача:": "personal_task",
        "личное дело:": "personal_task",
        "personal:": "personal_task",
        "personal task:": "personal_task",
        "рабочее:": "task",
        "рабочая задача:": "task",
        "рабочая задача ": "task",
        "событие:": "event",
        "в календарь:": "event",
        "event:": "event",
        "to calendar:": "event",
    }
    for marker, action in marker_map.items():
        if lowered.startswith(marker):
            return action
    if lowered.startswith("напомни"):
        return "reminder"
    if lowered.startswith("remind"):
        return "reminder"
    return None


def _strip_prefixed_capture(raw_text: str, *, action_hint: str | None) -> str:
    text = _clean(raw_text)
    hint = _clean(action_hint).lower()
    if not text:
        return text
    patterns: tuple[str, ...]
    if hint == "idea":
        patterns = (
            r"^\s*идея\s*[:\-]\s*(.+)$",
            r"^\s*идея\s+(.+)$",
            r"^\s*idea\s*[:\-]\s*(.+)$",
            r"^\s*idea\s+(.+)$",
        )
    elif hint == "personal_task":
        patterns = (
            r"^\s*личное\s*[:\-]\s*(.+)$",
            r"^\s*личная задача\s*[:\-]\s*(.+)$",
            r"^\s*личное дело\s*[:\-]\s*(.+)$",
            r"^\s*personal\s*[:\-]\s*(.+)$",
        )
    elif hint == "reminder":
        patterns = (
            r"^\s*напомни(?:\s+мне)?\s*[:,\-]?\s*(.+)$",
            r"^\s*remind(?:\s+me)?\s*[:,\-]?\s*(.+)$",
        )
    else:
        return text
    for pattern in patterns:
        match = re.match(pattern, text, flags=re.IGNORECASE | re.UNICODE)
        if match:
            return _clean(match.group(1))
    return text


def _local_explicit_reminder_intent(raw_text: str, tz_name: str) -> IntakeIntent | None:
    body = _strip_prefixed_capture(raw_text, action_hint="reminder")
    if not body:
        return _normalize_intake_payload({"action": "reminder", "reminder_text": "", "remind_at_local": None, "reply": ""})
    reminder_text, remind_local = quick_extract_datetime_ru(body, tz_name, prefer_future=True, date_only_time=None)
    if remind_local is None or not _clean(reminder_text):
        return None
    return _normalize_intake_payload(
        {
            "action": "reminder",
            "reminder_text": _clean(reminder_text),
            "remind_at_local": remind_local.strftime("%Y-%m-%d %H:%M"),
            "reply": "",
        }
    )


def _llm_fingerprint(action: str, **data: object) -> str:
    payload = {"action": _clean(action).lower()}
    for key, value in sorted(data.items()):
        if isinstance(value, datetime):
            payload[key] = value.isoformat()
        else:
            payload[key] = value
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


async def _find_recent_duplicate(
    db_pool: asyncpg.Pool,
    chat_id: int,
    fingerprint: str,
    *,
    conn: asyncpg.Connection | None = None,
) -> dict | None:
    if conn is not None:
        return await find_recent_action(conn, chat_id=int(chat_id), fingerprint=fingerprint)
    async with db_pool.acquire() as target_conn:
        return await find_recent_action(target_conn, chat_id=int(chat_id), fingerprint=fingerprint)


async def _remember_llm_action(*args, **kwargs) -> None:
    """Backward-compatible no-op kept for older tests and imports."""


async def _followup_context(
    state: FSMContext | None,
    *,
    db_pool: asyncpg.Pool,
    chat_id: int,
) -> dict[str, object]:
    if state is not None:
        try:
            current = await state.get_state()
            if current == FreeformFollowup.awaiting_text.state:
                data = await state.get_data()
                if isinstance(data, dict) and data:
                    return data
        except Exception:
            pass
    async with db_pool.acquire() as conn:
        persisted = await get_conversation_state(conn, int(chat_id), "freeform_followup")
    if not persisted:
        return {}
    return dict(persisted.get("payload") or {})


def _build_classification_user_prompt(
    *,
    raw_text: str,
    prepend_text: str | None,
    followup_data: dict[str, object],
) -> str:
    text = _clean(raw_text)
    base_text = _clean(followup_data.get("freeform_base_text") or prepend_text)
    pending_action = _clean(followup_data.get("freeform_pending_action")).lower()
    missing_fields = tuple(
        _clean(item)
        for item in (followup_data.get("freeform_missing_fields") or [])
        if _clean(item)
    )
    action_hint = _clean(followup_data.get("freeform_action_hint")).lower() or _clean(_action_hint_from_text(text)).lower() or _clean(_action_hint_from_text(base_text)).lower()

    if base_text and text and base_text != text:
        parts = [
            "This is a clarification turn for a previously incomplete request.",
            f"Original request:\n{base_text}",
        ]
        if pending_action:
            parts.append(f"Expected action: {pending_action}.")
        if missing_fields:
            parts.append(f"Still missing fields: {', '.join(missing_fields)}.")
        if action_hint:
            parts.append(f"Strong action hint: {action_hint}.")
        parts.append(f"User clarification:\n{text}")
        parts.append("Return one final executable JSON object for the combined request.")
        return "\n\n".join(parts)

    if action_hint:
        return f"Strong action hint: {action_hint}.\n\nUser message:\n{text or base_text}"
    return _merge_freeform_text(prepend_text, raw_text)


def _parse_duration_min(value: object) -> int | None:
    try:
        duration = int(value)
    except Exception:
        return None
    if 5 <= duration <= 12 * 60:
        return duration
    return None


def _event_summary(kind: str, title: str, project_code: str | None) -> str:
    if kind == "work":
        work_tpl = os.getenv("ICLOUD_WORK_SUMMARY_TEMPLATE", "{project_prefix}{title}")
        prefix = ""
        code = _clean(project_code)
        if code and not title.startswith(f"{code}:"):
            prefix = f"{code}: "
        return work_tpl.format(title=title, project=code, project_prefix=prefix).strip()
    personal_tpl = os.getenv("ICLOUD_PERSONAL_SUMMARY_TEMPLATE", "{title}")
    return personal_tpl.format(title=title, project="", project_prefix="").strip()


def _gtasks_error_toast(kind_label: str, exc: Exception) -> str:
    raw = _clean(exc)
    lower = raw.lower()
    if "authentication failed" in lower or "invalid_grant" in lower:
        detail = "ошибка авторизации Google Tasks. Проверьте refresh token."
    elif "not configured" in lower:
        detail = "Google Tasks не настроен."
    else:
        detail = raw or "неизвестная ошибка."
    if len(detail) > 160:
        detail = detail[:157].rstrip() + "..."
    return f"⚠️ Не удалось добавить {kind_label} в Google Tasks: {detail}"


async def _render_screen(
    message: Message,
    db_pool: asyncpg.Pool,
    deps: AppDeps,
    *,
    screen: str,
    payload: dict | None = None,
) -> int:
    tz_name = resolve_tz_name(deps.tz_name)
    async with db_pool.acquire() as conn:
        persona_mode = await get_persona_mode(conn, int(message.chat.id))
    if screen == "home":
        return await ui_render_home(message, db_pool, tz_name=tz_name, force_new=False)
    if screen == "projects":
        return await ui_render_projects_portfolio(message, db_pool, tz_name=tz_name, force_new=False)
    if screen == "today":
        return await ui_render_today(message, db_pool, tz_name=tz_name, icloud=deps.icloud, force_new=False)
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
        return await ui_render_help(message, db_pool, force_new=False)
    if screen == "add":
        return await ui_render_add_menu(message, db_pool, force_new=False)
    if screen == "team":
        if is_solo_mode(persona_mode):
            return await ui_render_home_more(message, db_pool, force_new=False)
        return await ui_render_team(message, db_pool, force_new=False)
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


async def _clear_followup_state(
    state: FSMContext | None,
    *,
    db_pool: asyncpg.Pool,
    chat_id: int,
) -> None:
    if state is None:
        pass
    else:
        try:
            current = await state.get_state()
        except Exception:
            current = None
        if current == FreeformFollowup.awaiting_text.state:
            await state.clear()
    async with db_pool.acquire() as conn:
        await clear_conversation_state(conn, int(chat_id), "freeform_followup")


async def _start_followup(
    message: Message,
    *,
    deps: AppDeps,
    db_pool: asyncpg.Pool,
    state: FSMContext | None,
    prompt: str,
    base_text: str,
    source: str,
    pending_action: str | None = None,
    missing_fields: tuple[str, ...] | list[str] = (),
) -> bool:
    prompt_text = _clean(prompt) or "\u0423\u0442\u043e\u0447\u043d\u0438\u0442\u0435 \u0437\u0430\u043f\u0440\u043e\u0441."
    payload = {
        "freeform_base_text": base_text,
        "freeform_source": source,
        "freeform_pending_action": _clean(pending_action).lower() or None,
        "freeform_missing_fields": [_clean(field) for field in missing_fields if _clean(field)],
        "freeform_action_hint": _action_hint_from_text(base_text),
    }
    if state is not None:
        await state.clear()
        await state.update_data(**payload)
        await state.set_state(FreeformFollowup.awaiting_text)
    async with db_pool.acquire() as conn:
        await set_conversation_state(
            conn,
            int(message.chat.id),
            "freeform_followup",
            step="awaiting_text",
            payload=payload,
            ttl_sec=max(300, int(os.getenv("FREEFORM_FOLLOWUP_TTL_SEC", "1800"))),
        )
    await _rerender_with_toast(message, db_pool, deps, prompt_text)
    return True


def _canon_phrase(value: object) -> str:
    return re.sub(r"[\W_]+", " ", canon(_clean(value)), flags=re.UNICODE).strip()


def _tokens(value: object) -> tuple[str, ...]:
    phrase = _canon_phrase(value)
    return tuple(token for token in phrase.split() if token)


def _contains_token_sequence(haystack: tuple[str, ...], needle: tuple[str, ...]) -> bool:
    if not haystack or not needle or len(needle) > len(haystack):
        return False
    width = len(needle)
    return any(haystack[idx : idx + width] == needle for idx in range(len(haystack) - width + 1))


def _same_name_stem(left: str, right: str) -> bool:
    a = _canon_phrase(left)
    b = _canon_phrase(right)
    if not a or not b:
        return False
    if a == b:
        return True
    for prefix_len in (4, 3):
        if min(len(a), len(b)) >= prefix_len and a[:prefix_len] == b[:prefix_len]:
            return True
    return False


def _pick_unique_best(scored: list[tuple[int, T]], *, min_score: int = 1) -> T | None:
    ranked = [(score, item) for score, item in scored if score >= min_score]
    if not ranked:
        return None
    ranked.sort(key=lambda item: item[0], reverse=True)
    if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
        return None
    return ranked[0][1]


def _match_project_option(
    projects: list[ProjectOption],
    *,
    requested_code: str | None,
    requested_name: str | None,
    raw_text: str,
) -> ProjectOption | None:
    hint_code = _clean(requested_code)
    hint_name = _clean(requested_name)
    raw_tokens = _tokens(raw_text)
    scored: list[tuple[int, ProjectOption]] = []

    for project in projects:
        project_code = _canon_phrase(project.code)
        project_name = _canon_phrase(project.name)
        project_code_tokens = _tokens(project.code)
        project_name_tokens = _tokens(project.name)
        score = 0

        if hint_code:
            code_hint = _canon_phrase(hint_code)
            if code_hint == project_code or code_hint == project_name:
                return project
            if code_hint and code_hint in project_name:
                score = max(score, 860)

        if hint_name:
            name_hint = _canon_phrase(hint_name)
            name_hint_tokens = _tokens(hint_name)
            if name_hint and (name_hint == project_name or name_hint == project_code):
                return project
            if name_hint and len(name_hint) >= 4 and name_hint in project_name:
                score = max(score, 840)
            if name_hint_tokens and set(name_hint_tokens).issubset(set(project_name_tokens)):
                score = max(score, 820 + len(name_hint_tokens))

        if project_code_tokens and _contains_token_sequence(raw_tokens, project_code_tokens):
            score = max(score, 720 + len(project_code_tokens))
        if project_name_tokens and len(project_name_tokens) >= 2 and _contains_token_sequence(raw_tokens, project_name_tokens):
            score = max(score, 700 + len(project_name_tokens))

        if score > 0:
            scored.append((score, project))

    return _pick_unique_best(scored, min_score=680)


def _match_assignee_option(
    team: list[TeamOption],
    *,
    requested_name: str | None,
    raw_text: str,
) -> TeamOption | None:
    hint_phrase = _canon_phrase(requested_name)
    hint_tokens = _tokens(requested_name)
    raw_phrase = _canon_phrase(raw_text)
    raw_tokens = _tokens(raw_text)

    first_name_counts: dict[str, int] = {}
    for member in team:
        first_name = next(iter(_tokens(member.name)), "")
        if first_name:
            first_name_counts[first_name] = first_name_counts.get(first_name, 0) + 1

    scored: list[tuple[int, TeamOption]] = []
    for member in team:
        member_phrase = _canon_phrase(member.name)
        member_tokens = _tokens(member.name)
        member_first_name = next(iter(member_tokens), "")
        score = 0

        if hint_phrase:
            if hint_phrase == member_phrase:
                return member
            if hint_phrase in member_phrase or member_phrase in hint_phrase:
                score = max(score, 900)
            if hint_tokens and set(hint_tokens).issubset(set(member_tokens)):
                score = max(score, 860 + len(hint_tokens))
            if len(hint_tokens) == 1 and hint_tokens[0] == member_first_name and first_name_counts.get(member_first_name) == 1:
                score = max(score, 850)
            if len(hint_tokens) == 1 and _same_name_stem(hint_tokens[0], member_first_name) and first_name_counts.get(member_first_name) == 1:
                score = max(score, 835)

        if member_phrase and f" {member_phrase} " in f" {raw_phrase} ":
            score = max(score, 760)
        if member_first_name and member_first_name in raw_tokens and first_name_counts.get(member_first_name) == 1:
            score = max(score, 720)
        if member_first_name and first_name_counts.get(member_first_name) == 1 and any(
            _same_name_stem(token, member_first_name) for token in raw_tokens
        ):
            score = max(score, 730)

        if score > 0:
            scored.append((score, member))

    return _pick_unique_best(scored, min_score=720)


async def _load_freeform_context(
    conn: asyncpg.Connection,
    *,
    chat_id: int,
) -> tuple[int | None, str | None, list[ProjectOption], list[TeamOption]]:
    current_project_id = await get_current_project_id(conn, int(chat_id))
    project_rows = await conn.fetch(
        "SELECT id, code, name FROM projects WHERE status='active' ORDER BY CASE WHEN id=$1 THEN 0 ELSE 1 END, code",
        int(current_project_id or 0),
    )
    team_rows = await conn.fetch("SELECT id, name FROM team ORDER BY name")

    projects = [
        ProjectOption(id=int(row["id"]), code=str(row["code"] or ""), name=str(row["name"] or ""))
        for row in project_rows
    ]
    team = [TeamOption(id=int(row["id"]), name=str(row["name"] or "")) for row in team_rows]

    current_project_code = next((item.code for item in projects if item.id == int(current_project_id or 0)), None)
    if current_project_id and not current_project_code:
        current_project_code = await conn.fetchval("SELECT code FROM projects WHERE id=$1", int(current_project_id))

    return int(current_project_id) if current_project_id else None, current_project_code, projects, team


async def _resolve_project(
    conn: asyncpg.Connection,
    *,
    requested_code: str | None,
    requested_name: str | None,
    raw_text: str,
    current_project_id: int | None,
    projects: list[ProjectOption],
) -> tuple[int | None, str | None, str | None]:
    match = _match_project_option(
        projects,
        requested_code=requested_code,
        requested_name=requested_name,
        raw_text=raw_text,
    )
    if match is not None:
        return match.id, match.code, None

    requested_label = _clean(requested_code or requested_name)
    if requested_label:
        return None, None, f"\u041d\u0435 \u043d\u0430\u0448\u0451\u043b \u043f\u0440\u043e\u0435\u043a\u0442 {requested_label}."

    inbox_project = next((project for project in projects if project.code.upper() == "INBOX"), None)
    if inbox_project is not None:
        return inbox_project.id, inbox_project.code, None

    inbox_id = await ensure_inbox_project_id(conn)
    return int(inbox_id), "INBOX", None


def _resolve_assignee(
    *,
    requested_name: str | None,
    raw_text: str,
    team: list[TeamOption],
) -> tuple[int | None, str | None, str | None]:
    requested_label = _clean(requested_name)
    if requested_label.lower() in {"none", "no assignee"}:
        return None, None, None

    match = _match_assignee_option(team, requested_name=requested_name, raw_text=raw_text)
    if match is not None:
        return match.id, match.name, None
    if requested_label:
        return None, None, f"\u041d\u0435 \u043d\u0430\u0448\u0451\u043b \u0438\u0441\u043f\u043e\u043b\u043d\u0438\u0442\u0435\u043b\u044f {requested_label}."
    return None, None, None


def _intake_system_prompt(
    *,
    now_local: datetime,
    tz_name: str,
    current_project_code: str | None,
    projects: list[ProjectOption],
    team: list[TeamOption],
) -> str:
    project = current_project_code or "INBOX"
    project_lines = "\n".join(f"- {item.code}: {item.name}" for item in projects[:80]) or "- INBOX: Inbox"
    team_lines = "\n".join(f"- {item.name}" for item in team[:80]) or "- none"
    return (
        "You route user messages for a personal task assistant. "
        "Return JSON only, without markdown.\n"
        f"User local time: {now_local.strftime('%Y-%m-%d %H:%M')} ({tz_name}). "
        f"Current project: {project}.\n"
        "Allowed actions: task, personal_task, reminder, event, idea, reply.\n"
        "For action=task return:\n"
        "- title: concise actionable task title without assignee/project/deadline boilerplate;\n"
        "- deadline_local: YYYY-MM-DD HH:MM or null;\n"
        "- project_code: exact code from AVAILABLE_PROJECTS or null;\n"
        "- project_name: mentioned project name if the user referenced a project but exact code is uncertain; else null;\n"
        "- assignee_name: exact person name from AVAILABLE_TEAM if the user referenced an assignee; else null.\n"
        "For action=personal_task return:\n"
        "- title: concise personal todo title;\n"
        "- deadline_local: YYYY-MM-DD HH:MM or null.\n"
        "For action=reminder return reminder_text and remind_at_local (YYYY-MM-DD HH:MM).\n"
        "For action=event return:\n"
        "- title: concise event title;\n"
        "- calendar_kind: work or personal;\n"
        "- start_at_local: YYYY-MM-DD HH:MM;\n"
        "- duration_min: integer duration in minutes;\n"
        "- project_code/project_name only when the work event clearly belongs to a project.\n"
        "For action=idea return idea_text with the raw idea to store in Google Tasks.\n"
        "Use action=reply only if the request is not actionable or required data is missing.\n"
        "Prefer reminder when the user explicitly asks to remind. "
        "Prefer event for meetings, calls, appointments, calendar bookings, and time blocks. "
        "Prefer idea for thoughts, concepts, brainstorm items, things to capture without a deadline. "
        "Prefer personal_task for personal todos, errands, purchases, and household tasks tracked in Google Tasks. "
        "Prefer task for actionable work items.\n"
        "If the user prompt includes 'Strong action hint: <action>', follow it unless the request is clearly impossible.\n"
        "Never invent project codes or team members outside the provided lists.\n"
        "AVAILABLE_PROJECTS:\n"
        f"{project_lines}\n"
        "AVAILABLE_TEAM:\n"
        f"{team_lines}\n"
        "Keep reply under 120 characters."
    )


async def handle_freeform_text(
    message: Message,
    *,
    deps: AppDeps,
    db_pool: asyncpg.Pool,
    raw_text: str,
    source: str = "text",
    state: FSMContext | None = None,
    prepend_text: str | None = None,
) -> bool:
    llm = getattr(deps, "llm", None)
    if llm is None or not getattr(llm, "enabled", False):
        return False

    followup_data = await _followup_context(state, db_pool=db_pool, chat_id=int(message.chat.id))
    text = _clean(raw_text)
    if not text:
        return False

    tz_name = resolve_tz_name(deps.tz_name)
    tz = ZoneInfo(tz_name)
    current_project_id: int | None = None
    current_project_code: str | None = None
    projects: list[ProjectOption] = []
    team: list[TeamOption] = []
    persona_mode = "lead"
    async with db_pool.acquire() as conn:
        persona_mode = await get_persona_mode(conn, int(message.chat.id))
        current_project_id, current_project_code, projects, team = await _load_freeform_context(
            conn,
            chat_id=int(message.chat.id),
        )
    if is_solo_mode(persona_mode):
        team = []

    try:
        base_text = _clean(followup_data.get("freeform_base_text") or prepend_text)
        action_hint = (
            _clean(_action_hint_from_text(text)).lower()
            or _clean(followup_data.get("freeform_action_hint")).lower()
            or _clean(_action_hint_from_text(base_text)).lower()
        )
        if action_hint == "idea":
            intent = _normalize_intake_payload(
                {
                    "action": "idea",
                    "idea_text": _strip_prefixed_capture(text, action_hint=action_hint),
                    "reply": "",
                }
            )
        elif action_hint == "personal_task":
            intent = _normalize_intake_payload(
                {
                    "action": "personal_task",
                    "title": _strip_prefixed_capture(text, action_hint=action_hint),
                    "reply": "",
                }
            )
        elif action_hint == "reminder":
            intent = _local_explicit_reminder_intent(text, tz_name)
            if intent is None:
                user_prompt = _build_classification_user_prompt(
                    raw_text=text,
                    prepend_text=prepend_text,
                    followup_data=followup_data,
                )
                payload = await llm.classify_intake(
                    system_prompt=_intake_system_prompt(
                        now_local=datetime.now(tz),
                        tz_name=tz_name,
                        current_project_code=current_project_code,
                        projects=projects,
                        team=team,
                    ),
                    user_prompt=user_prompt,
                )
                intent = _normalize_intake_payload(payload)
        else:
            user_prompt = _build_classification_user_prompt(
                raw_text=text,
                prepend_text=prepend_text,
                followup_data=followup_data,
            )
            payload = await llm.classify_intake(
                system_prompt=_intake_system_prompt(
                    now_local=datetime.now(tz),
                    tz_name=tz_name,
                    current_project_code=current_project_code,
                    projects=projects,
                    team=team,
                ),
                user_prompt=user_prompt,
            )
            intent = _normalize_intake_payload(payload)
    except Exception:
        logger.exception("freeform classify failed", extra={"source": source})
        return False

    if intent.needs_followup and intent.followup_prompt:
        return await _start_followup(
            message,
            deps=deps,
            db_pool=db_pool,
            state=state,
            prompt=intent.followup_prompt,
            base_text=_clean(followup_data.get("freeform_base_text") or prepend_text or text),
            source=source,
            pending_action=intent.followup_action,
            missing_fields=intent.missing_fields,
        )

    if intent.action == "task":
        if is_solo_mode(persona_mode):
            intent.assignee_name = None
        deadline_local = _parse_local_dt(intent.deadline_local, tz_name) if intent.deadline_local else None
        if intent.deadline_local and deadline_local is None:
            return await _start_followup(
                message,
                deps=deps,
                db_pool=db_pool,
                state=state,
                prompt="\u041d\u0435 \u0441\u043c\u043e\u0433 \u0440\u0430\u0437\u043e\u0431\u0440\u0430\u0442\u044c \u0441\u0440\u043e\u043a \u0437\u0430\u0434\u0430\u0447\u0438. \u0423\u0442\u043e\u0447\u043d\u0438\u0442\u0435 \u0434\u0430\u0442\u0443 \u0438 \u0432\u0440\u0435\u043c\u044f.",
                base_text=_clean(followup_data.get("freeform_base_text") or prepend_text or text),
                source=source,
                pending_action="task",
                missing_fields=("deadline_local",),
            )
        try:
            async with db_pool.acquire() as conn:
                project_id, project_code, project_error = await _resolve_project(
                    conn,
                    requested_code=intent.project_code,
                    requested_name=intent.project_name,
                    raw_text=text,
                    current_project_id=current_project_id,
                    projects=projects,
                )
                if project_error or project_id is None:
                    return await _start_followup(
                        message,
                        deps=deps,
                        db_pool=db_pool,
                        state=state,
                        prompt=(project_error or "\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043e\u043f\u0440\u0435\u0434\u0435\u043b\u0438\u0442\u044c \u043f\u0440\u043e\u0435\u043a\u0442 \u0434\u043b\u044f \u0437\u0430\u0434\u0430\u0447\u0438.")
                        + " \u0423\u0442\u043e\u0447\u043d\u0438\u0442\u0435 \u043a\u043e\u0434 \u0438\u043b\u0438 \u043d\u0430\u0437\u0432\u0430\u043d\u0438\u0435 \u043f\u0440\u043e\u0435\u043a\u0442\u0430.",
                        base_text=_clean(followup_data.get("freeform_base_text") or prepend_text or text),
                        source=source,
                        pending_action="task",
                        missing_fields=("project_code",),
                    )
                assignee_id, assignee_name, assignee_error = _resolve_assignee(
                    requested_name=intent.assignee_name,
                    raw_text=text,
                    team=team,
                )
            if assignee_error and not is_solo_mode(persona_mode):
                return await _start_followup(
                    message,
                    deps=deps,
                    db_pool=db_pool,
                    state=state,
                    prompt=assignee_error + " \u0423\u0442\u043e\u0447\u043d\u0438\u0442\u0435 \u0438\u043c\u044f \u0438\u043b\u0438 \u0443\u0431\u0435\u0440\u0438\u0442\u0435 \u0438\u0441\u043f\u043e\u043b\u043d\u0438\u0442\u0435\u043b\u044f.",
                    base_text=_clean(followup_data.get("freeform_base_text") or prepend_text or text),
                    source=source,
                    pending_action="task",
                    missing_fields=("assignee_name",),
                )
            task_fingerprint = _llm_fingerprint(
                "task",
                title=intent.title,
                project_id=int(project_id),
                assignee_id=assignee_id,
                deadline=deadline_local,
            )
            duplicate = await _find_recent_duplicate(db_pool, int(message.chat.id), task_fingerprint)
            if duplicate:
                await _clear_followup_state(state, db_pool=db_pool, chat_id=int(message.chat.id))
                await _rerender_with_toast(message, db_pool, deps, "Похожий черновик уже есть. Подтвердите или отмените его.")
                return True
            await _clear_followup_state(state, db_pool=db_pool, chat_id=int(message.chat.id))
            await create_pending_preview(
                message,
                db_pool=db_pool,
                deps=deps,
                kind="task",
                payload={
                    "title": intent.title,
                    "project_id": int(project_id),
                    "project_code": project_code,
                    "assignee_id": assignee_id,
                    "assignee_name": assignee_name,
                    "deadline_local": deadline_local.isoformat() if deadline_local else "",
                },
                fingerprint=task_fingerprint,
                summary=intent.title,
                source=source,
            )
            return True
        except Exception:
            logger.exception("freeform task draft failed", extra={"source": source})
            return False

    if intent.action == "personal_task":
        gtasks = getattr(deps, "gtasks", None)
        if gtasks is None or not gtasks.enabled():
            await _clear_followup_state(state, db_pool=db_pool, chat_id=int(message.chat.id))
            await _rerender_with_toast(
                message,
                db_pool,
                deps,
                "\u274c Google Tasks \u043d\u0435 \u043d\u0430\u0441\u0442\u0440\u043e\u0435\u043d \u0434\u043b\u044f \u043b\u0438\u0447\u043d\u044b\u0445 \u0437\u0430\u0434\u0430\u0447.",
            )
            return True

        due_local = _parse_local_dt(intent.deadline_local, tz_name) if intent.deadline_local else None
        if intent.deadline_local and due_local is None:
            return await _start_followup(
                message,
                deps=deps,
                db_pool=db_pool,
                state=state,
                prompt="\u041d\u0435 \u0441\u043c\u043e\u0433 \u0440\u0430\u0437\u043e\u0431\u0440\u0430\u0442\u044c \u0441\u0440\u043e\u043a \u043b\u0438\u0447\u043d\u043e\u0439 \u0437\u0430\u0434\u0430\u0447\u0438. \u0423\u0442\u043e\u0447\u043d\u0438\u0442\u0435 \u0434\u0430\u0442\u0443 \u0438\u043b\u0438 \u0443\u0431\u0435\u0440\u0438\u0442\u0435 \u0441\u0440\u043e\u043a.",
                base_text=_clean(followup_data.get("freeform_base_text") or prepend_text or text),
                source=source,
                pending_action="personal_task",
                missing_fields=("deadline_local",),
            )

        try:
            personal_fingerprint = _llm_fingerprint("personal_task", title=intent.title, due=due_local)
            duplicate = await _find_recent_duplicate(db_pool, int(message.chat.id), personal_fingerprint)
            if duplicate:
                await _clear_followup_state(state, db_pool=db_pool, chat_id=int(message.chat.id))
                await _rerender_with_toast(message, db_pool, deps, "Похожий черновик уже есть. Подтвердите или отмените его.")
                return True
            await _clear_followup_state(state, db_pool=db_pool, chat_id=int(message.chat.id))
            await create_pending_preview(
                message,
                db_pool=db_pool,
                deps=deps,
                kind="personal_task",
                payload={
                    "title": intent.title,
                    "deadline_local": due_local.isoformat() if due_local else "",
                },
                fingerprint=personal_fingerprint,
                summary=intent.title,
                source=source,
            )
            return True
        except Exception as exc:
            logger.exception("freeform personal task draft failed", extra={"source": source})
            await _clear_followup_state(state, db_pool=db_pool, chat_id=int(message.chat.id))
            await _rerender_with_toast(message, db_pool, deps, _gtasks_error_toast("личную задачу", exc))
            return True

    if intent.action == "event":
        start_local = _parse_local_dt(intent.start_at_local, tz_name)
        if start_local is None:
            return await _start_followup(
                message,
                deps=deps,
                db_pool=db_pool,
                state=state,
                prompt="\u041d\u0435 \u0441\u043c\u043e\u0433 \u0440\u0430\u0437\u043e\u0431\u0440\u0430\u0442\u044c \u0432\u0440\u0435\u043c\u044f \u0441\u043e\u0431\u044b\u0442\u0438\u044f. \u0423\u043a\u0430\u0436\u0438\u0442\u0435 \u0434\u0430\u0442\u0443 \u0438 \u0432\u0440\u0435\u043c\u044f.",
                base_text=_clean(followup_data.get("freeform_base_text") or prepend_text or text),
                source=source,
                pending_action="event",
                missing_fields=("start_at_local",),
            )
        duration_min = _parse_duration_min(intent.duration_min)
        if duration_min is None:
            return await _start_followup(
                message,
                deps=deps,
                db_pool=db_pool,
                state=state,
                prompt="\u0423\u0442\u043e\u0447\u043d\u0438\u0442\u0435 \u0434\u043b\u0438\u0442\u0435\u043b\u044c\u043d\u043e\u0441\u0442\u044c \u0441\u043e\u0431\u044b\u0442\u0438\u044f \u0432 \u043c\u0438\u043d\u0443\u0442\u0430\u0445.",
                base_text=_clean(followup_data.get("freeform_base_text") or prepend_text or text),
                source=source,
                pending_action="event",
                missing_fields=("duration_min",),
            )
        if start_local <= datetime.now(tz):
            return await _start_followup(
                message,
                deps=deps,
                db_pool=db_pool,
                state=state,
                prompt="\u0412\u0440\u0435\u043c\u044f \u0441\u043e\u0431\u044b\u0442\u0438\u044f \u0443\u0436\u0435 \u043f\u0440\u043e\u0448\u043b\u043e. \u0423\u043a\u0430\u0436\u0438\u0442\u0435 \u0431\u0443\u0434\u0443\u0449\u0435\u0435 \u0432\u0440\u0435\u043c\u044f.",
                base_text=_clean(followup_data.get("freeform_base_text") or prepend_text or text),
                source=source,
                pending_action="event",
                missing_fields=("start_at_local",),
            )
        if not (os.getenv("ICLOUD_APPLE_ID", "").strip() and os.getenv("ICLOUD_APP_PASSWORD", "").strip()):
            await _clear_followup_state(state, db_pool=db_pool, chat_id=int(message.chat.id))
            await _rerender_with_toast(
                message,
                db_pool,
                deps,
                "\u274c iCloud CalDAV \u043d\u0435 \u043d\u0430\u0441\u0442\u0440\u043e\u0435\u043d.",
            )
            return True

        kind = intent.calendar_kind or "personal"
        if kind == "work":
            cal_url = os.getenv("ICLOUD_CALENDAR_URL_WORK", "").strip()
            if not cal_url:
                await _clear_followup_state(state, db_pool=db_pool, chat_id=int(message.chat.id))
                await _rerender_with_toast(
                    message,
                    db_pool,
                    deps,
                    "\u274c \u041d\u0435 \u0437\u0430\u0434\u0430\u043d ICLOUD_CALENDAR_URL_WORK \u0434\u043b\u044f \u0440\u0430\u0431\u043e\u0447\u0438\u0445 \u0441\u043e\u0431\u044b\u0442\u0438\u0439.",
                )
                return True
        else:
            cal_url = os.getenv("ICLOUD_CALENDAR_URL_PERSONAL", "").strip()
            if not cal_url:
                await _clear_followup_state(state, db_pool=db_pool, chat_id=int(message.chat.id))
                await _rerender_with_toast(
                    message,
                    db_pool,
                    deps,
                    "\u274c \u041d\u0435 \u0437\u0430\u0434\u0430\u043d ICLOUD_CALENDAR_URL_PERSONAL \u0434\u043b\u044f \u043b\u0438\u0447\u043d\u044b\u0445 \u0441\u043e\u0431\u044b\u0442\u0438\u0439.",
                )
                return True

        project_id: int | None = None
        project_code: str | None = None
        if kind == "work":
            try:
                async with db_pool.acquire() as conn:
                    project_id, project_code, project_error = await _resolve_project(
                        conn,
                        requested_code=intent.project_code,
                        requested_name=intent.project_name,
                        raw_text=text,
                        current_project_id=current_project_id,
                        projects=projects,
                    )
                    if project_error or project_id is None:
                        return await _start_followup(
                            message,
                            deps=deps,
                            db_pool=db_pool,
                            state=state,
                            prompt=(project_error or "\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043e\u043f\u0440\u0435\u0434\u0435\u043b\u0438\u0442\u044c \u043f\u0440\u043e\u0435\u043a\u0442 \u0434\u043b\u044f \u0441\u043e\u0431\u044b\u0442\u0438\u044f.")
                            + " \u0423\u0442\u043e\u0447\u043d\u0438\u0442\u0435 \u043a\u043e\u0434 \u0438\u043b\u0438 \u043d\u0430\u0437\u0432\u0430\u043d\u0438\u0435 \u043f\u0440\u043e\u0435\u043a\u0442\u0430.",
                            base_text=_clean(followup_data.get("freeform_base_text") or prepend_text or text),
                            source=source,
                            pending_action="event",
                            missing_fields=("project_code",),
                        )
            except Exception:
                logger.exception("freeform event project resolve failed", extra={"source": source})
                return False

        summary = _event_summary(kind, intent.title, project_code)
        dtstart_utc = start_local.astimezone(timezone.utc)
        try:
            event_fingerprint = _llm_fingerprint(
                "event",
                kind=kind,
                title=intent.title,
                project_id=project_id,
                start=dtstart_utc,
                duration_min=duration_min,
            )
            duplicate = await _find_recent_duplicate(db_pool, int(message.chat.id), event_fingerprint)
            if duplicate:
                await _clear_followup_state(state, db_pool=db_pool, chat_id=int(message.chat.id))
                await _rerender_with_toast(message, db_pool, deps, "Похожий черновик уже есть. Подтвердите или отмените его.")
                return True
            await _clear_followup_state(state, db_pool=db_pool, chat_id=int(message.chat.id))
            await create_pending_preview(
                message,
                db_pool=db_pool,
                deps=deps,
                kind="event",
                payload={
                    "title": intent.title,
                    "calendar_kind": kind,
                    "calendar_url": cal_url,
                    "summary": summary,
                    "start_local": start_local.isoformat(),
                    "duration_min": int(duration_min),
                    "project_id": int(project_id) if project_id else None,
                    "project_code": project_code,
                },
                fingerprint=event_fingerprint,
                summary=summary,
                source=source,
            )
            return True
        except Exception:
            logger.exception("freeform event draft failed", extra={"source": source})
            return False

    if intent.action == "idea":
        gtasks = getattr(deps, "gtasks", None)
        if gtasks is None or not gtasks.enabled():
            await _clear_followup_state(state, db_pool=db_pool, chat_id=int(message.chat.id))
            await _rerender_with_toast(
                message,
                db_pool,
                deps,
                "\u274c Google Tasks \u043d\u0435 \u043d\u0430\u0441\u0442\u0440\u043e\u0435\u043d \u0434\u043b\u044f \u0438\u0434\u0435\u0439.",
            )
            return True

        ideas_list = os.getenv("GTASKS_IDEAS_LIST", "\u0418\u0434\u0435\u0438")
        try:
            idea_fingerprint = _llm_fingerprint("idea", idea_text=intent.idea_text)
            duplicate = await _find_recent_duplicate(db_pool, int(message.chat.id), idea_fingerprint)
            if duplicate:
                await _clear_followup_state(state, db_pool=db_pool, chat_id=int(message.chat.id))
                await _rerender_with_toast(message, db_pool, deps, "Похожий черновик уже есть. Подтвердите или отмените его.")
                return True
            await _clear_followup_state(state, db_pool=db_pool, chat_id=int(message.chat.id))
            await create_pending_preview(
                message,
                db_pool=db_pool,
                deps=deps,
                kind="idea",
                payload={"idea_text": intent.idea_text},
                fingerprint=idea_fingerprint,
                summary=intent.idea_text,
                source=source,
            )
            return True
        except Exception as exc:
            logger.exception("freeform idea draft failed", extra={"source": source})
            await _clear_followup_state(state, db_pool=db_pool, chat_id=int(message.chat.id))
            await _rerender_with_toast(message, db_pool, deps, _gtasks_error_toast("идею", exc))
            return True

    if intent.action == "reminder":
        remind_local = _parse_local_dt(intent.remind_at_local, tz_name)
        if remind_local is None:
            return await _start_followup(
                message,
                deps=deps,
                db_pool=db_pool,
                state=state,
                prompt="\u041d\u0435 \u0441\u043c\u043e\u0433 \u0440\u0430\u0437\u043e\u0431\u0440\u0430\u0442\u044c \u0434\u0430\u0442\u0443 \u043d\u0430\u043f\u043e\u043c\u0438\u043d\u0430\u043d\u0438\u044f. \u0423\u043a\u0430\u0436\u0438\u0442\u0435 \u0434\u0430\u0442\u0443 \u0438 \u0432\u0440\u0435\u043c\u044f.",
                base_text=_clean(followup_data.get("freeform_base_text") or prepend_text or text),
                source=source,
                pending_action="reminder",
                missing_fields=("remind_at_local",),
            )
        if remind_local <= datetime.now(tz):
            return await _start_followup(
                message,
                deps=deps,
                db_pool=db_pool,
                state=state,
                prompt="\u0412\u0440\u0435\u043c\u044f \u043d\u0430\u043f\u043e\u043c\u0438\u043d\u0430\u043d\u0438\u044f \u0443\u0436\u0435 \u043f\u0440\u043e\u0448\u043b\u043e. \u0423\u043a\u0430\u0436\u0438\u0442\u0435 \u0431\u0443\u0434\u0443\u0449\u0435\u0435 \u0432\u0440\u0435\u043c\u044f.",
                base_text=_clean(followup_data.get("freeform_base_text") or prepend_text or text),
                source=source,
                pending_action="reminder",
                missing_fields=("remind_at_local",),
            )
        try:
            reminder_fingerprint = _llm_fingerprint("reminder", text=intent.reminder_text, remind_at=remind_local)
            duplicate = await _find_recent_duplicate(db_pool, int(message.chat.id), reminder_fingerprint)
            if duplicate:
                await _clear_followup_state(state, db_pool=db_pool, chat_id=int(message.chat.id))
                await _rerender_with_toast(message, db_pool, deps, "Похожий черновик уже есть. Подтвердите или отмените его.")
                return True
            await _clear_followup_state(state, db_pool=db_pool, chat_id=int(message.chat.id))
            await create_pending_preview(
                message,
                db_pool=db_pool,
                deps=deps,
                kind="reminder",
                payload={
                    "reminder_text": intent.reminder_text,
                    "remind_at_local": remind_local.isoformat(),
                },
                fingerprint=reminder_fingerprint,
                summary=intent.reminder_text,
                source=source,
            )
            return True
        except Exception:
            logger.exception("freeform reminder draft failed", extra={"source": source})
            return False

    reply = intent.reply or "\u041d\u0435 \u043f\u043e\u043d\u044f\u043b \u0437\u0430\u043f\u0440\u043e\u0441."
    await _clear_followup_state(state, db_pool=db_pool, chat_id=int(message.chat.id))
    await _rerender_with_toast(message, db_pool, deps, reply)
    return True


async def handle_freeform_voice(
    message: Message,
    *,
    deps: AppDeps,
    db_pool: asyncpg.Pool,
    state: FSMContext | None = None,
    prepend_text: str | None = None,
) -> bool:
    llm = getattr(deps, "llm", None)
    if llm is None or not getattr(llm, "enabled", False):
        return False

    file_id, filename, mime_type, file_size = _voice_file_meta(message)
    if not file_id:
        return False

    max_bytes = int(os.getenv("LLM_VOICE_MAX_BYTES", str(8 * 1024 * 1024)))
    if file_size and file_size > max_bytes:
        await _rerender_with_toast(
            message,
            db_pool,
            deps,
            "\u0413\u043e\u043b\u043e\u0441\u043e\u0432\u043e\u0435 \u0441\u043b\u0438\u0448\u043a\u043e\u043c \u0431\u043e\u043b\u044c\u0448\u043e\u0435. \u041e\u0442\u043f\u0440\u0430\u0432\u044c\u0442\u0435 \u043a\u043e\u0440\u043e\u0447\u0435 \u0438\u043b\u0438 \u0442\u0435\u043a\u0441\u0442\u043e\u043c.",
        )
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
        await _rerender_with_toast(
            message,
            db_pool,
            deps,
            "\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u0440\u0430\u0441\u043f\u043e\u0437\u043d\u0430\u0442\u044c \u0433\u043e\u043b\u043e\u0441\u043e\u0432\u043e\u0435 \u0441\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u0435.",
        )
        return True

    return await handle_freeform_text(
        message,
        deps=deps,
        db_pool=db_pool,
        raw_text=transcript,
        source="voice",
        state=state,
        prepend_text=prepend_text,
    )


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
