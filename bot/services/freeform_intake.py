"""LLM-based free-form intake for text and voice messages."""

from __future__ import annotations

import io
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import TypeVar
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
    deadline_local: str | None = None
    reminder_text: str = ""
    remind_at_local: str | None = None
    project_code: str | None = None
    project_name: str | None = None
    assignee_name: str | None = None
    screen: str | None = None
    reply: str = ""


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
    project_name = _clean(data.get("project_name")) or None
    assignee_name = _clean(data.get("assignee_name")) or None
    screen = _normalize_screen(data.get("screen"))

    if action == "task" and not title:
        action = "reply"
        reply = reply or "\u041d\u0435 \u0441\u043c\u043e\u0433 \u0441\u043e\u0431\u0440\u0430\u0442\u044c \u0437\u0430\u0434\u0430\u0447\u0443 \u0438\u0437 \u0441\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u044f."
    if action == "reminder" and (not reminder_text or not _clean(data.get("remind_at_local"))):
        action = "reply"
        reply = reply or "\u041d\u0435 \u0441\u043c\u043e\u0433 \u0441\u043e\u0431\u0440\u0430\u0442\u044c \u043d\u0430\u043f\u043e\u043c\u0438\u043d\u0430\u043d\u0438\u0435 \u0438\u0437 \u0441\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u044f."
    if action == "nav" and not screen:
        action = "reply"
        reply = reply or "\u041d\u0435 \u043f\u043e\u043d\u044f\u043b, \u043a\u0430\u043a\u043e\u0439 \u044d\u043a\u0440\u0430\u043d \u043d\u0443\u0436\u043d\u043e \u043e\u0442\u043a\u0440\u044b\u0442\u044c."

    return IntakeIntent(
        action=action,
        title=title,
        deadline_local=_clean(data.get("deadline_local")) or None,
        reminder_text=reminder_text,
        remind_at_local=_clean(data.get("remind_at_local")) or None,
        project_code=project_code,
        project_name=project_name,
        assignee_name=assignee_name,
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

    if current_project_id:
        current_project = next((project for project in projects if project.id == int(current_project_id)), None)
        if current_project is not None:
            return current_project.id, current_project.code, None

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
        "Allowed actions: task, reminder, nav, reply.\n"
        "For action=task return:\n"
        "- title: concise actionable task title without assignee/project/deadline boilerplate;\n"
        "- deadline_local: YYYY-MM-DD HH:MM or null;\n"
        "- project_code: exact code from AVAILABLE_PROJECTS or null;\n"
        "- project_name: mentioned project name if the user referenced a project but exact code is uncertain; else null;\n"
        "- assignee_name: exact person name from AVAILABLE_TEAM if the user referenced an assignee; else null.\n"
        "For action=reminder return reminder_text and remind_at_local (YYYY-MM-DD HH:MM).\n"
        "For action=nav return screen from: home, projects, today, overdue, all_tasks, work, inbox, help, add, team, stats.\n"
        "Use action=reply only if the request is not actionable or required data is missing.\n"
        "Prefer reminder when the user explicitly asks to remind. Prefer nav for show/open/list screen requests. "
        "Prefer task for actionable work items.\n"
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
) -> bool:
    llm = getattr(deps, "llm", None)
    if llm is None or not getattr(llm, "enabled", False):
        return False

    text = _clean(raw_text)
    if not text:
        return False

    tz_name = resolve_tz_name(deps.tz_name)
    tz = ZoneInfo(tz_name)
    current_project_id: int | None = None
    current_project_code: str | None = None
    projects: list[ProjectOption] = []
    team: list[TeamOption] = []
    async with db_pool.acquire() as conn:
        current_project_id, current_project_code, projects, team = await _load_freeform_context(
            conn,
            chat_id=int(message.chat.id),
        )

    try:
        payload = await llm.classify_intake(
            system_prompt=_intake_system_prompt(
                now_local=datetime.now(tz),
                tz_name=tz_name,
                current_project_code=current_project_code,
                projects=projects,
                team=team,
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
                    raise ValueError(project_error or "\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043e\u043f\u0440\u0435\u0434\u0435\u043b\u0438\u0442\u044c \u043f\u0440\u043e\u0435\u043a\u0442 \u0434\u043b\u044f \u0437\u0430\u0434\u0430\u0447\u0438.")

                assignee_id, assignee_name, assignee_error = _resolve_assignee(
                    requested_name=intent.assignee_name,
                    raw_text=text,
                    team=team,
                )
                if assignee_error:
                    raise ValueError(assignee_error)

                task_id = await conn.fetchval(
                    "INSERT INTO tasks (project_id, title, assignee_id, deadline) VALUES ($1,$2,$3,$4) RETURNING id",
                    int(project_id),
                    intent.title,
                    assignee_id,
                    to_db_utc(
                        deadline_local,
                        tz_name=tz_name,
                        store_tz=bool(getattr(deps, "db_tasks_deadline_timestamptz", False)),
                    )
                    if deadline_local
                    else None,
                )
                event_text = f"LLM/{source}: [{project_code}] #{int(task_id)} {intent.title}"
                if assignee_name:
                    event_text += f" -> {assignee_name}"
                await db_add_event(
                    conn,
                    "task_created",
                    int(project_id),
                    int(task_id),
                    event_text,
                )
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

        meta_parts: list[str] = []
        if project_code:
            meta_parts.append(f"[{project_code}]")
        if assignee_name:
            meta_parts.append(assignee_name)
        if deadline_local:
            meta_parts.append("\u0434\u043e " + fmt_local(to_db_utc(deadline_local, tz_name=tz_name, store_tz=False), tz))
        toast = f"\u2705 \u0417\u0430\u0434\u0430\u0447\u0430: {intent.title}"
        if meta_parts:
            toast += " \u00b7 " + " \u00b7 ".join(meta_parts)
        await _rerender_with_toast(message, db_pool, deps, toast)
        return True

    if intent.action == "reminder":
        remind_local = _parse_local_dt(intent.remind_at_local, tz_name)
        if remind_local is None:
            await _rerender_with_toast(message, db_pool, deps, "\u041d\u0435 \u0441\u043c\u043e\u0433 \u0440\u0430\u0437\u043e\u0431\u0440\u0430\u0442\u044c \u0434\u0430\u0442\u0443 \u043d\u0430\u043f\u043e\u043c\u0438\u043d\u0430\u043d\u0438\u044f.")
            return True
        if remind_local <= datetime.now(tz):
            await _rerender_with_toast(
                message,
                db_pool,
                deps,
                "\u0412\u0440\u0435\u043c\u044f \u043d\u0430\u043f\u043e\u043c\u0438\u043d\u0430\u043d\u0438\u044f \u0443\u0436\u0435 \u043f\u0440\u043e\u0448\u043b\u043e. \u0423\u043a\u0430\u0436\u0438\u0442\u0435 \u0431\u0443\u0434\u0443\u0449\u0435\u0435 \u0432\u0440\u0435\u043c\u044f.",
            )
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
        await _rerender_with_toast(message, db_pool, deps, f"\u2705 \u041d\u0430\u043f\u043e\u043c\u0438\u043d\u0430\u043d\u0438\u0435: {when_txt}")
        return True

    reply = intent.reply or "\u041d\u0435 \u043f\u043e\u043d\u044f\u043b \u0437\u0430\u043f\u0440\u043e\u0441."
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
