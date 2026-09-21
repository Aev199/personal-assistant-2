"""UI-independent intake for native Assistant clients.

Uses the same Gemini-first classifier and domain rules as Telegram, but returns
structured results instead of rendering Telegram UI.
"""
from __future__ import annotations

import os
import secrets
from dataclasses import asdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import asyncpg

from bot.db import ensure_inbox_project_id, get_persona_mode
from bot.db.runtime_state import (
    create_pending_action,
    find_recent_action,
    forget_recent_action,
    get_pending_action,
    mark_pending_action_status,
    remember_recent_action,
    set_conversation_state,
)
from bot.persona import is_solo_mode
from bot.services import freeform_intake as intake
from bot.services.pending_actions import execute_pending_action
from bot.tz import resolve_tz_name


SAFE_INSTANT_KINDS = frozenset({"task", "personal_task", "reminder", "idea"})


def _label(intent) -> str:
    if intent.action == "reminder":
        return intent.reminder_text
    if intent.action == "idea":
        return intent.idea_text
    return intent.title


async def _save_receipt(
    db_pool: asyncpg.Pool,
    *,
    chat_id: int,
    capture_id: str,
    raw_text: str,
    source: str,
    items: list[dict] | None = None,
) -> None:
    async with db_pool.acquire() as conn:
        await set_conversation_state(
            conn,
            int(chat_id),
            f"native_capture:{capture_id}",
            step="saved",
            payload={
                "raw_text": raw_text,
                "source": source,
                "items": items or [],
            },
            ttl_sec=None,
        )


async def _load_context(db_pool: asyncpg.Pool, *, chat_id: int):
    async with db_pool.acquire() as conn:
        persona_mode = await get_persona_mode(conn, int(chat_id))
        current_project_id, current_project_code, projects, team = await intake._load_freeform_context(
            conn,
            chat_id=int(chat_id),
        )
    if is_solo_mode(persona_mode):
        team = []
    return persona_mode, current_project_id, current_project_code, projects, team


async def _classify(
    text: str,
    *,
    deps,
    current_project_code,
    projects,
    team,
    prepend_text: str | None = None,
):
    llm = getattr(deps, "llm", None)
    if llm is None or not getattr(llm, "enabled", False):
        raise RuntimeError("llm_unavailable")

    tz_name = resolve_tz_name(deps.tz_name)
    tz = ZoneInfo(tz_name)
    action_hint = intake._action_hint_from_text(text)

    if action_hint == "idea":
        return [
            intake._normalize_intake_payload(
                {
                    "action": "idea",
                    "idea_text": intake._strip_prefixed_capture(text, action_hint=action_hint),
                    "reply": "",
                }
            )
        ]
    if action_hint == "personal_task":
        return [
            intake._normalize_intake_payload(
                {
                    "action": "personal_task",
                    "title": intake._strip_prefixed_capture(text, action_hint=action_hint),
                    "reply": "",
                }
            )
        ]
    if action_hint == "reminder":
        local = intake._local_explicit_reminder_intent(text, tz_name)
        if local is not None:
            return [local]

    prompt = intake._build_classification_user_prompt(
        raw_text=text,
        prepend_text=prepend_text,
        followup_data={},
    )

    if not prepend_text and not action_hint and intake._is_complex_message(text) and hasattr(llm, "classify_intake_batch"):
        response = await llm.classify_intake_batch(
            system_prompt=intake._intake_system_prompt_batch(
                now_local=datetime.now(tz),
                tz_name=tz_name,
                current_project_code=current_project_code,
                projects=projects,
                team=team,
            ),
            user_prompt=prompt,
        )
        intents, _ = intake._normalize_batch_payloads(response, retain_incomplete=True)
        return intents

    response = await llm.classify_intake(
        system_prompt=intake._intake_system_prompt(
            now_local=datetime.now(tz),
            tz_name=tz_name,
            current_project_code=current_project_code,
            projects=projects,
            team=team,
        ),
        user_prompt=prompt,
    )
    return [intake._normalize_intake_payload(response)]


async def _prepare_action(
    intent,
    *,
    text: str,
    deps,
    db_pool: asyncpg.Pool,
    chat_id: int,
    persona_mode: str,
    current_project_id,
    projects,
    team,
):
    tz_name = resolve_tz_name(deps.tz_name)
    tz = ZoneInfo(tz_name)

    if intent.needs_followup:
        return None, intent.followup_prompt or intent.reply or "Нужно уточнение."

    if intent.action == "task":
        if is_solo_mode(persona_mode):
            intent.assignee_name = None
        deadline = intake._parse_local_dt(intent.deadline_local, tz_name) if intent.deadline_local else None
        if intent.deadline_local and deadline is None:
            return None, "Уточните срок задачи."

        async with db_pool.acquire() as conn:
            project_id, project_code, project_error = await intake._resolve_project(
                conn,
                requested_code=intent.project_code,
                requested_name=intent.project_name,
                raw_text=text,
                current_project_id=current_project_id,
                projects=projects,
            )
            if project_id is None:
                project_id = await ensure_inbox_project_id(conn)
                project_code = "INBOX"

        assignee_id, assignee_name, _ = intake._resolve_assignee(
            requested_name=intent.assignee_name,
            raw_text=text,
            team=team,
        )
        fingerprint = intake._llm_fingerprint(
            "task",
            title=intent.title,
            project_id=int(project_id),
            assignee_id=assignee_id,
            deadline=deadline,
        )
        return {
            "kind": "task",
            "summary": intent.title,
            "fingerprint": fingerprint,
            "payload": {
                "title": intent.title,
                "project_id": int(project_id),
                "project_code": project_code,
                "assignee_id": assignee_id,
                "assignee_name": assignee_name,
                "deadline_local": deadline.isoformat() if deadline else "",
            },
        }, None

    if intent.action == "personal_task":
        due = intake._parse_local_dt(intent.deadline_local, tz_name) if intent.deadline_local else None
        if intent.deadline_local and due is None:
            return None, "Уточните срок личной задачи."
        return {
            "kind": "personal_task",
            "summary": intent.title,
            "fingerprint": intake._llm_fingerprint("personal_task", title=intent.title, due=due),
            "payload": {
                "title": intent.title,
                "deadline_local": due.isoformat() if due else "",
            },
        }, None

    if intent.action == "idea":
        return {
            "kind": "idea",
            "summary": intent.idea_text,
            "fingerprint": intake._llm_fingerprint("idea", idea_text=intent.idea_text),
            "payload": {"idea_text": intent.idea_text},
        }, None

    if intent.action == "reminder":
        remind_at = intake._parse_local_dt(intent.remind_at_local, tz_name)
        if remind_at is None:
            return None, intent.followup_prompt or "Когда напомнить?"
        if remind_at <= datetime.now(tz):
            return None, "Укажите будущее время напоминания."
        return {
            "kind": "reminder",
            "summary": intent.reminder_text,
            "fingerprint": intake._llm_fingerprint(
                "reminder",
                text=intent.reminder_text,
                remind_at=remind_at,
            ),
            "payload": {
                "reminder_text": intent.reminder_text,
                "remind_at_local": remind_at.isoformat(),
            },
        }, None

    if intent.action == "event":
        start = intake._parse_local_dt(intent.start_at_local, tz_name)
        duration = intake._parse_duration_min(intent.duration_min)
        if start is None:
            return None, "Уточните дату и время события."
        if duration is None:
            return None, "Сколько продлится событие?"
        if start <= datetime.now(tz):
            return None, "Укажите будущее время события."

        kind = intent.calendar_kind or "personal"
        cal_url = os.getenv(
            "ICLOUD_CALENDAR_URL_WORK" if kind == "work" else "ICLOUD_CALENDAR_URL_PERSONAL",
            "",
        ).strip()
        if not cal_url:
            return None, "Календарь для этого события не настроен."

        project_id = None
        project_code = None
        if kind == "work":
            async with db_pool.acquire() as conn:
                project_id, project_code, _ = await intake._resolve_project(
                    conn,
                    requested_code=intent.project_code,
                    requested_name=intent.project_name,
                    raw_text=text,
                    current_project_id=current_project_id,
                    projects=projects,
                )
                if project_id is None:
                    project_id = await ensure_inbox_project_id(conn)
                    project_code = "INBOX"

        summary = intake._event_summary(kind, intent.title, project_code)
        return {
            "kind": "event",
            "summary": summary,
            "fingerprint": intake._llm_fingerprint(
                "event",
                kind=kind,
                title=intent.title,
                project_id=project_id,
                start=start.astimezone(timezone.utc),
                duration_min=duration,
            ),
            "payload": {
                "title": intent.title,
                "calendar_kind": kind,
                "calendar_url": cal_url,
                "summary": summary,
                "start_local": start.isoformat(),
                "duration_min": int(duration),
                "project_id": int(project_id) if project_id else None,
                "project_code": project_code,
            },
        }, None

    return None, intent.reply or "Не понял, что нужно сделать."


async def _persist_action(
    spec: dict,
    *,
    deps,
    db_pool: asyncpg.Pool,
    chat_id: int,
    source: str,
):
    fingerprint = str(spec["fingerprint"])
    async with db_pool.acquire() as conn:
        duplicate = await find_recent_action(conn, chat_id=int(chat_id), fingerprint=fingerprint)
        if duplicate:
            return {
                "status": "duplicate",
                "kind": spec["kind"],
                "title": spec["summary"],
                "pending_action_id": duplicate.get("pending_action_id"),
            }

        stored_payload = {**spec["payload"], "source": source}
        pending_id = await create_pending_action(
            conn,
            chat_id=int(chat_id),
            kind=str(spec["kind"]),
            payload=stored_payload,
            source_message_id=0,
            fingerprint=fingerprint,
            ttl_sec=900,
        )
        await remember_recent_action(
            conn,
            chat_id=int(chat_id),
            fingerprint=fingerprint,
            action=str(spec["kind"]),
            summary=str(spec["summary"]),
            pending_action_id=int(pending_id),
            ttl_sec=900,
        )

    if spec["kind"] not in SAFE_INSTANT_KINDS:
        return {
            "status": "needs_confirmation",
            "kind": spec["kind"],
            "title": spec["summary"],
            "pending_action_id": int(pending_id),
            "payload": spec["payload"],
        }

    try:
        await execute_pending_action(
            {
                "id": int(pending_id),
                "kind": spec["kind"],
                "payload": {**spec["payload"], "source": source},
                "fingerprint": fingerprint,
            },
            db_pool=db_pool,
            deps=deps,
            chat_id=int(chat_id),
        )
        return {
            "status": "saved",
            "kind": spec["kind"],
            "title": spec["summary"],
            "pending_action_id": int(pending_id),
        }
    except Exception as exc:
        async with db_pool.acquire() as conn:
            await mark_pending_action_status(
                conn,
                pending_action_id=int(pending_id),
                status="failed",
                last_error=str(exc),
            )
            await forget_recent_action(conn, chat_id=int(chat_id), fingerprint=fingerprint)
        raise


async def process_native_capture(
    *,
    text: str,
    deps,
    db_pool: asyncpg.Pool,
    chat_id: int,
    prepend_text: str | None = None,
    source: str = "ios",
) -> dict:
    text = str(text or "").strip()
    if not text:
        return {"ok": False, "error": "empty_text"}

    capture_id = secrets.token_hex(8)
    await _save_receipt(
        db_pool,
        chat_id=int(chat_id),
        capture_id=capture_id,
        raw_text=text,
        source=source,
    )

    persona_mode, current_project_id, current_project_code, projects, team = await _load_context(
        db_pool,
        chat_id=int(chat_id),
    )

    try:
        intents = await _classify(
            text,
            deps=deps,
            current_project_code=current_project_code,
            projects=projects,
            team=team,
            prepend_text=prepend_text,
        )
    except Exception as exc:
        await _save_receipt(
            db_pool,
            chat_id=int(chat_id),
            capture_id=capture_id,
            raw_text=text,
            source=source,
            items=[{"status": "unprocessed", "error": str(exc)}],
        )
        return {
            "ok": True,
            "capture_id": capture_id,
            "status": "stored",
            "message": "Записано. Разбор временно недоступен.",
            "saved": [],
            "needs_input": [],
            "pending": [],
        }

    saved = []
    needs_input = []
    pending = []
    receipt_items = []

    for intent in intents:
        spec, prompt = await _prepare_action(
            intent,
            text=text,
            deps=deps,
            db_pool=db_pool,
            chat_id=int(chat_id),
            persona_mode=persona_mode,
            current_project_id=current_project_id,
            projects=projects,
            team=team,
        )
        if spec is None:
            item = {
                "status": "needs_input",
                "action": intent.followup_action or intent.action,
                "title": _label(intent),
                "prompt": prompt or "Нужно уточнение.",
            }
            needs_input.append(item)
            receipt_items.append({"intent": asdict(intent), **item})
            continue

        try:
            result = await _persist_action(
                spec,
                deps=deps,
                db_pool=db_pool,
                chat_id=int(chat_id),
                source=source,
            )
        except Exception:
            result = {
                "status": "failed",
                "kind": spec["kind"],
                "title": spec["summary"],
            }

        receipt_items.append({"intent": asdict(intent), **result})
        if result["status"] in {"saved", "duplicate"}:
            saved.append(result)
        elif result["status"] == "needs_confirmation":
            pending.append(result)
        else:
            needs_input.append(
                {
                    "status": "needs_input",
                    "action": spec["kind"],
                    "title": spec["summary"],
                    "prompt": "Не удалось сохранить. Попробуйте ещё раз.",
                }
            )

    await _save_receipt(
        db_pool,
        chat_id=int(chat_id),
        capture_id=capture_id,
        raw_text=text,
        source=source,
        items=receipt_items,
    )

    if needs_input:
        status = "partial" if saved or pending else "needs_input"
    elif pending:
        status = "needs_confirmation"
    else:
        status = "saved"

    provider = getattr(getattr(deps, "llm", None), "last_provider", None)
    return {
        "ok": True,
        "capture_id": capture_id,
        "status": status,
        "saved": saved,
        "needs_input": needs_input,
        "pending": pending,
        "provider": provider,
        "context": intake._merge_freeform_text(prepend_text, text),
    }


async def confirm_native_action(
    *,
    pending_action_id: int,
    deps,
    db_pool: asyncpg.Pool,
    chat_id: int,
) -> dict:
    async with db_pool.acquire() as conn:
        pending = await get_pending_action(
            conn,
            chat_id=int(chat_id),
            pending_action_id=int(pending_action_id),
        )
    if not pending:
        return {"ok": False, "error": "pending_action_not_found"}
    if pending["status"] == "executed":
        return {"ok": True, "status": "saved", "pending_action_id": int(pending_action_id)}
    if pending["status"] != "pending":
        return {"ok": False, "error": f"pending_action_{pending['status']}"}

    try:
        result = await execute_pending_action(
            pending,
            db_pool=db_pool,
            deps=deps,
            chat_id=int(chat_id),
        )
    except Exception as exc:
        async with db_pool.acquire() as conn:
            await mark_pending_action_status(
                conn,
                pending_action_id=int(pending_action_id),
                status="failed",
                last_error=str(exc),
            )
        return {"ok": False, "error": "execute_failed"}

    return {
        "ok": True,
        "status": "saved",
        "pending_action_id": int(pending_action_id),
        "message": result,
    }


async def cancel_native_action(
    *,
    pending_action_id: int,
    db_pool: asyncpg.Pool,
    chat_id: int,
) -> dict:
    async with db_pool.acquire() as conn:
        pending = await get_pending_action(
            conn,
            chat_id=int(chat_id),
            pending_action_id=int(pending_action_id),
        )
        if not pending:
            return {"ok": False, "error": "pending_action_not_found"}
        if pending["status"] == "pending":
            await mark_pending_action_status(
                conn,
                pending_action_id=int(pending_action_id),
                status="cancelled",
            )
            fingerprint = str(pending.get("fingerprint") or "")
            if fingerprint:
                await forget_recent_action(conn, chat_id=int(chat_id), fingerprint=fingerprint)
    return {"ok": True, "status": "cancelled", "pending_action_id": int(pending_action_id)}
