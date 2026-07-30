"""Lossless persistence for the initial brain-dump onboarding flow.

Kept separate from the UI handler so import policy is testable and can evolve
without making the Telegram conversation module larger.
"""

from __future__ import annotations

import os
from datetime import datetime
from zoneinfo import ZoneInfo

import asyncpg
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot.db import ensure_inbox_project_id
from bot.services.onboarding_state import mark_onboarding_complete
from bot.tz import resolve_tz_name


async def save_onboarding(
    message: Message,
    *,
    state: FSMContext,
    db_pool: asyncpg.Pool,
    deps,
    create_projects: bool,
) -> tuple[int, list[str], int]:
    """Persist every usable onboarding item.

    Existing projects are always respected. Suggested new projects are created
    only when ``create_projects`` is true. Items whose specialised integration
    is unavailable or fails are preserved as normal Inbox tasks.
    """
    from bot.handlers import onboarding as flow

    data = await state.get_data()
    intents = flow._deserialize_intents(list(data.get("onboarding_intents") or []))
    if not intents:
        raise RuntimeError("Черновик наполнения потерян. Запустите мастер заново.")

    tz_name = resolve_tz_name(deps.tz_name)
    tz = ZoneInfo(tz_name)
    async with db_pool.acquire() as conn:
        _, _, existing_projects, _ = await flow._load_freeform_context(
            conn,
            chat_id=int(message.chat.id),
        )
        inbox_id = await ensure_inbox_project_id(conn)
        existing_ids = {int(project.id) for project in existing_projects}
        project_map = {
            flow._project_key(project.code): project for project in existing_projects
        }
        project_map.update(
            {flow._project_key(project.name): project for project in existing_projects}
        )

        created_projects = 0
        if create_projects:
            for name in flow._suggested_project_names(intents, existing_projects):
                project = await flow._create_project(conn, name)
                project_map[flow._project_key(project.name)] = project
                project_map[flow._project_key(project.code)] = project
                if int(project.id) not in existing_ids:
                    existing_ids.add(int(project.id))
                    created_projects += 1

        inbox_row = await conn.fetchrow(
            "SELECT id, code, name FROM projects WHERE id=$1",
            int(inbox_id),
        )
        inbox = flow.ProjectOption(
            int(inbox_row["id"]),
            str(inbox_row["code"]),
            str(inbox_row["name"]),
        )

    async def save_internal_task(title: str, *, project=None, deadline=None) -> None:
        destination = project or inbox
        await flow._execute_import_item(
            message=message,
            db_pool=db_pool,
            deps=deps,
            kind="task",
            payload={
                "title": title,
                "project_id": int(destination.id),
                "project_code": destination.code,
                "assignee_id": None,
                "assignee_name": None,
                "deadline_local": deadline.isoformat() if deadline else "",
            },
            summary=title,
        )

    saved = 0
    errors: list[str] = []
    gtasks_ready = bool(deps.gtasks is not None and deps.gtasks.enabled())
    icloud_ready = bool(
        os.getenv("ICLOUD_APPLE_ID", "").strip()
        and os.getenv("ICLOUD_APP_PASSWORD", "").strip()
    )

    for intent in intents:
        try:
            project = (
                project_map.get(flow._project_key(intent.project_code))
                or project_map.get(flow._project_key(intent.project_name))
                or flow._existing_project(intent, existing_projects)
                or inbox
            )

            if intent.action == "task":
                deadline = (
                    flow._parse_local_dt(intent.deadline_local, tz_name)
                    if intent.deadline_local
                    else None
                )
                await save_internal_task(intent.title, project=project, deadline=deadline)

            elif intent.action == "personal_task":
                due = (
                    flow._parse_local_dt(intent.deadline_local, tz_name)
                    if intent.deadline_local
                    else None
                )
                if gtasks_ready:
                    try:
                        await flow._execute_import_item(
                            message=message,
                            db_pool=db_pool,
                            deps=deps,
                            kind="personal_task",
                            payload={
                                "title": intent.title,
                                "deadline_local": due.isoformat() if due else "",
                            },
                            summary=intent.title,
                        )
                    except Exception:
                        await save_internal_task(intent.title, project=inbox, deadline=due)
                else:
                    await save_internal_task(intent.title, project=inbox, deadline=due)

            elif intent.action == "idea":
                idea = intent.idea_text or intent.title
                if gtasks_ready:
                    try:
                        await flow._execute_import_item(
                            message=message,
                            db_pool=db_pool,
                            deps=deps,
                            kind="idea",
                            payload={"idea_text": idea},
                            summary=idea,
                        )
                    except Exception:
                        await save_internal_task(f"Идея: {idea}", project=inbox)
                else:
                    await save_internal_task(f"Идея: {idea}", project=inbox)

            elif intent.action == "reminder":
                reminder_text = intent.reminder_text or intent.title
                remind_at = flow._parse_local_dt(intent.remind_at_local, tz_name)
                if remind_at is not None and remind_at > datetime.now(tz):
                    try:
                        await flow._execute_import_item(
                            message=message,
                            db_pool=db_pool,
                            deps=deps,
                            kind="reminder",
                            payload={
                                "reminder_text": reminder_text,
                                "remind_at_local": remind_at.isoformat(),
                            },
                            summary=reminder_text,
                        )
                    except Exception:
                        await save_internal_task(
                            f"Напомнить: {reminder_text}",
                            project=inbox,
                            deadline=remind_at,
                        )
                else:
                    await save_internal_task(
                        f"Напомнить: {reminder_text}",
                        project=inbox,
                    )

            elif intent.action == "event":
                start = flow._parse_local_dt(intent.start_at_local, tz_name)
                calendar_kind = intent.calendar_kind or "personal"
                calendar_url = os.getenv(
                    "ICLOUD_CALENDAR_URL_WORK"
                    if calendar_kind == "work"
                    else "ICLOUD_CALENDAR_URL_PERSONAL",
                    "",
                ).strip()
                destination = project if calendar_kind == "work" else inbox
                can_create_event = bool(
                    icloud_ready
                    and start
                    and start > datetime.now(tz)
                    and calendar_url
                )
                if can_create_event:
                    project_code = project.code if calendar_kind == "work" else None
                    summary = flow._event_summary(
                        calendar_kind,
                        intent.title,
                        project_code,
                    )
                    try:
                        await flow._execute_import_item(
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
                                "project_id": int(project.id)
                                if calendar_kind == "work"
                                else None,
                                "project_code": project_code,
                            },
                            summary=summary,
                        )
                    except Exception:
                        await save_internal_task(
                            intent.title,
                            project=destination,
                            deadline=start,
                        )
                else:
                    await save_internal_task(
                        intent.title,
                        project=destination,
                        deadline=start,
                    )

            else:
                title = flow._intent_label(intent) or "Неопознанная запись"
                await save_internal_task(title, project=inbox)

            saved += 1
        except Exception as exc:
            errors.append(str(exc)[:180])

    if saved:
        async with db_pool.acquire() as conn:
            await mark_onboarding_complete(conn, int(message.chat.id))
    return saved, errors, created_projects


def install_onboarding_import() -> None:
    """Bind the persistence service into the already imported UI flow."""
    from bot.handlers import onboarding as flow

    flow._save_onboarding = save_onboarding
