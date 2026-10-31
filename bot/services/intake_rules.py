"""Parsing rules for free-form capture, shared by every client.

The model returns an intent; these helpers normalise it, resolve projects and
people, and build prompts. Unknown projects go to Inbox and unknown people are
left unassigned, so capture never stops to ask about them.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, TypeVar
from zoneinfo import ZoneInfo

import asyncpg

from bot.db import ensure_inbox_project_id, get_current_project_id
from bot.utils.datetime import parse_datetime_ru, quick_extract_datetime_ru
from bot.utils.text import canon

logger = logging.getLogger(__name__)


T = TypeVar("T")


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
    if _clean(requested_code or requested_name):
        logger.info("unresolved project routed to Inbox", extra={"requested": requested_code or requested_name})

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
        logger.info("unresolved assignee left empty", extra={"requested": requested_label})
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
        "- title: concise actionable task title. "
        "Do NOT include project name/code in title (it is set via project_code). "
        "Strip phrases like 'по проекту X', 'для проекта Y', 'в проекте Z' from the title;\n"
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
        "For action=idea return idea_text with the raw non-actionable thought to store in Assistant.\n"
        "Use action=reply only if the request is not actionable or required data is missing.\n"
        "Use reminder ONLY when the user explicitly asks to be reminded (напомни, напомнить, remind). "
        "A call, errand or to-do with a day but without such a request is a task with deadline_local "
        "on that day (18:00 when no time is given), never a reminder. "
        "Prefer event for meetings and appointments that have a start time (встреча, созвон в 15:00). "
        "Prefer idea for thoughts, concepts, brainstorm items, things to capture without a deadline. "
        "Prefer personal_task for personal todos, errands, purchases, and household tasks. "
        "Prefer task for actionable work items.\n"
        "If the user prompt includes 'Strong action hint: <action>', follow it unless the request is clearly impossible.\n"
        "Never invent project codes or team members outside the provided lists.\n"
        "AVAILABLE_PROJECTS:\n"
        f"{project_lines}\n"
        "AVAILABLE_TEAM:\n"
        f"{team_lines}\n"
        "Keep reply under 120 characters."
    )


def _intake_system_prompt_batch(
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
        "You are a personal task assistant router. "
        "The user sent a message that may contain MULTIPLE independent items "
        "(tasks, events, reminders, ideas, personal tasks). "
        "Break each item into a separate entry in the 'actions' array. "
        "If the message contains only one actionable item, return an array with one element. "
        "If nothing is actionable, return an empty actions array and a short reply explaining why.\n\n"
        "Return JSON only, without markdown.\n"
        f"User local time: {now_local.strftime('%Y-%m-%d %H:%M')} ({tz_name}). "
        f"Current project: {project}.\n\n"
        "Each action object must have:\n"
        "- action: one of task, personal_task, reminder, event, idea\n"
        "- title: concise title (for tasks/events/personal tasks; for reminders use reminder_text; for ideas use idea_text)\n\n"
        "For action=task include:\n"
        "- title: concise actionable task title. "
        "Do NOT include project name/code in the title (it is set via project_code). "
        "Strip phrases like 'по проекту X', 'для проекта Y' from the title;\n"
        "- deadline_local: YYYY-MM-DD HH:MM or null\n"
        "- project_code: exact code from AVAILABLE_PROJECTS or null\n"
        "- project_name: name if uncertain about code; else null\n"
        "- assignee_name: person name from AVAILABLE_TEAM or null\n\n"
        "For action=personal_task include:\n"
        "- deadline_local: YYYY-MM-DD HH:MM or null\n\n"
        "For action=reminder include:\n"
        "- title (used as reminder_text)\n"
        "- remind_at_local: YYYY-MM-DD HH:MM\n\n"
        "For action=event include:\n"
        "- calendar_kind: work or personal\n"
        "- start_at_local: YYYY-MM-DD HH:MM\n"
        "- duration_min: integer minutes\n"
        "- project_code/project_name only for work events\n\n"
        "For action=idea include:\n"
        "- title (used as idea_text)\n\n"
        "Rules:\n"
        "- Use reminder ONLY when the user explicitly asks to be reminded (напомни, напомнить, remind); "
        "a call or to-do with a day but no such request is a task due that day (18:00 when no time).\n"
        "- Prefer event for meetings and appointments that have a start time.\n"
        "- Prefer idea for thoughts, concepts, brainstorm items without a deadline.\n"
        "- Prefer personal_task for personal todos, errands, purchases, household tasks.\n"
        "- Prefer task for actionable work items.\n"
        "- Never invent project codes or team members outside the provided lists.\n"
        "- If a field is not applicable, omit it or set to null.\n"
        "- Do not invent missing dates, times, people, or duration. Keep incomplete items with null fields.\n"
        "- Keep personal and work items separate, even if their destinations are not configured.\n"
        "- Keep the 'reply' field under 200 characters. Describe classification, never claim anything was saved.\n\n"
        "AVAILABLE_PROJECTS:\n"
        f"{project_lines}\n\n"
        "AVAILABLE_TEAM:\n"
        f"{team_lines}"
    )


def _is_complex_message(text: str) -> bool:
    """Detect if a message likely contains multiple independent intents.

    Heuristics:
    - Numbered list (1. 2. 3.) with at least 2 items
    - 3+ substantial sentences (split by .,;\\n) → complex
    - 2 sentences with different action keywords → complex
    - 2 sentences with different project codes → complex
    - 2 sentences with low word overlap (distinct topics) → complex
    """
    t = _clean(text)

    # Check for numbered list pattern (at least 2 items)
    if len(re.findall(r"(?:^|\n)\s*\d+[\.\)]\s+", t)) >= 2:
        return True

    # Split into substantial sentences (min 8 chars — catches short clauses like "купить хлеб")
    sentences = [s.strip() for s in re.split(r"[.,;\n]+", t) if len(s.strip()) > 8]
    if len(sentences) < 2:
        return False

    # Short message with multiple substantial sentences → complex regardless of length
    # (e.g. "купить хлеб, зайти в аптеку" = 26 chars)
    if len(sentences) >= 3:
        return True

    # Exactly 2 sentences — check for different action types
    action_keywords: dict[str, str] = {
        "напомни": "reminder", "remind": "reminder",
        "задача": "task", "task": "task", "сделай": "task", "поставь": "task",
        "встреча": "event", "meeting": "event", "созвон": "event", "звонок": "event",
        "идея": "idea", "idea": "idea",
        "личное": "personal_task", "personal": "personal_task",
        "купи": "personal_task", "buy": "personal_task", "купить": "personal_task",
        "зайти": "personal_task", "заехать": "personal_task", "забрать": "personal_task",
        "сходить": "personal_task", "отсканить": "personal_task", "распечатать": "personal_task",
        "оплатить": "personal_task", "позвонить": "task", "отправить": "task",
    }
    found_actions: set[str] = set()
    for sentence in sentences:
        lower = canon(sentence)
        for kw, act in action_keywords.items():
            if kw in lower:
                found_actions.add(act)
    if len(found_actions) >= 2:
        return True

    # Same action type but different entities (projects, people) → complex
    project_code_re = re.compile(r"\b[A-Z]{1,6}[-_]?\d+\b|\b[A-Z]{2,6}\b")
    codes_per_sentence = [
        set(project_code_re.findall(s)) for s in sentences
    ]
    if codes_per_sentence[0] and codes_per_sentence[1] and codes_per_sentence[0] != codes_per_sentence[1]:
        return True

    # Two sentences with low word overlap → distinct topics (last resort)
    # Only when we have 0-1 action types (otherwise keyword signals are enough).
    if len(found_actions) <= 1:
        words0 = set(canon(sentences[0]).split())
        words1 = set(canon(sentences[1]).split())
        if words0 and words1:
            intersection = words0 & words1
            union = words0 | words1
            overlap = len(intersection) / len(union) if union else 0
            if overlap < 0.30:
                return True

    return False


def _normalize_batch_payloads(raw_response: dict[str, Any], *, retain_incomplete: bool = False) -> tuple[list[IntakeIntent], str]:
    """Convert batch classification response into a list of IntakeIntent + reply."""
    actions_data = raw_response.get("actions") or []
    if not isinstance(actions_data, list):
        actions_data = []
    reply = _clean(raw_response.get("reply") or "")
    intents: list[IntakeIntent] = []
    for item in actions_data:
        if not isinstance(item, dict):
            continue
        # Add dummy reply for normalize compatibility; batch items don't have individual reply
        item = dict(item)
        item.setdefault("reply", "")
        if item.get("action") == "reminder":
            item.setdefault("reminder_text", item.get("title") or "")
        intent = _normalize_intake_payload(item)
        if intent.action != "reply" or (retain_incomplete and intent.needs_followup):
            intents.append(intent)
    return intents, reply


_REMIND_WORDS = re.compile(r"напомн|remind", re.IGNORECASE)


def _reminders_only_when_asked(intents: list[IntakeIntent], *, text: str) -> list[IntakeIntent]:
    """A reminder pings at a set time, so it is created only when the user asks to be reminded.

    «Позвонить подрядчику завтра» is a task due tomorrow, whatever the model chose;
    «напомни завтра в 10 позвонить» stays a reminder.
    """
    if _REMIND_WORDS.search(text or ""):
        return intents
    result: list[IntakeIntent] = []
    for intent in intents:
        if intent.action == "reminder":
            intent = replace(
                intent,
                action="task",
                title=intent.reminder_text or intent.title,
                deadline_local=intent.remind_at_local or intent.deadline_local,
            )
        elif intent.needs_followup and intent.followup_action == "reminder":
            intent = replace(
                intent,
                action="task",
                title=intent.reminder_text or intent.title or _clean(text),
                needs_followup=False,
                followup_prompt="",
                followup_action=None,
                missing_fields=(),
            )
        result.append(intent)
    return result
