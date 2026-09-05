"""Low-friction product mode for the single-user assistant.

This module keeps the existing reliable storage/execution code, but changes the
runtime product behavior:

* safe free-form captures execute immediately and remain undoable;
* unknown project/assignee references degrade gracefully to Inbox/unassigned;
* batch captures show a completion summary instead of another confirmation;
* the regular tick may send one morning brief and one evening Inbox nudge.

The integration is intentionally installed from ``bot.bootstrap`` so the legacy
handlers and tests can still import their original functions without side
effects.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

import asyncpg
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.db import ensure_inbox_project_id
from bot.db.runtime_state import (
    create_pending_action,
    forget_recent_action,
    mark_pending_action_status,
    remember_recent_action,
)
from bot.services.pending_actions import execute_pending_action
from bot.tz import resolve_tz_name


logger = logging.getLogger(__name__)

SAFE_INSTANT_KINDS = frozenset({"task", "personal_task", "reminder", "idea"})

_OriginalPreview = Callable[..., Awaitable[int]]
_OriginalResolveProject = Callable[..., Awaitable[tuple[int | None, str | None, str | None]]]
_OriginalResolveAssignee = Callable[..., tuple[int | None, str | None, str | None]]
_OriginalTick = Callable[..., Awaitable[dict[str, object]]]

_original_create_pending_preview: _OriginalPreview | None = None
_original_resolve_project: _OriginalResolveProject | None = None
_original_resolve_assignee: _OriginalResolveAssignee | None = None
_original_do_tick: _OriginalTick | None = None
_installed = False


def _env_enabled(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_hour(name: str, default: int) -> int:
    try:
        return min(23, max(0, int(os.getenv(name, str(default)))))
    except Exception:
        return default


def _morning_due(
    now_local: datetime,
    *,
    last_sent: date | None,
    morning_hour: int,
    evening_hour: int,
) -> bool:
    return last_sent != now_local.date() and morning_hour <= now_local.hour < evening_hour


def _evening_due(
    now_local: datetime,
    *,
    last_sent: date | None,
    evening_hour: int,
) -> bool:
    return last_sent != now_local.date() and now_local.hour >= evening_hour


def _action_word(count: int) -> str:
    n = abs(int(count)) % 100
    if 11 <= n <= 19:
        return "действий"
    n %= 10
    if n == 1:
        return "действие"
    if 2 <= n <= 4:
        return "действия"
    return "действий"


async def _instant_create_pending_preview(
    message: Message,
    *,
    db_pool: asyncpg.Pool,
    deps,
    kind: str,
    payload: dict[str, Any],
    fingerprint: str,
    summary: str,
    source: str,
    force_new: bool = False,
) -> int:
    """Execute safe captures immediately, retaining the existing undo journal."""

    original = _original_create_pending_preview
    if original is None:
        raise RuntimeError("product mode is not installed")

    if not _env_enabled("ASSISTANT_INSTANT_CAPTURE", True) or kind not in SAFE_INSTANT_KINDS:
        return await original(
            message,
            db_pool=db_pool,
            deps=deps,
            kind=kind,
            payload=payload,
            fingerprint=fingerprint,
            summary=summary,
            source=source,
            force_new=force_new,
        )

    ttl_sec = max(60, int(os.getenv("PENDING_ACTION_TTL_SEC", "900")))
    chat_id = int(message.chat.id)
    stored_payload = {**payload, "source": source}
    pending_action_id = 0

    try:
        async with db_pool.acquire() as conn:
            pending_action_id = await create_pending_action(
                conn,
                chat_id=chat_id,
                kind=kind,
                payload=stored_payload,
                source_message_id=int(getattr(message, "message_id", 0) or 0),
                fingerprint=fingerprint,
                ttl_sec=ttl_sec,
            )
            await remember_recent_action(
                conn,
                chat_id=chat_id,
                fingerprint=fingerprint,
                action=kind,
                summary=summary,
                pending_action_id=int(pending_action_id),
                ttl_sec=max(45, ttl_sec),
            )

        result = await execute_pending_action(
            {
                "id": int(pending_action_id),
                "kind": kind,
                "payload": stored_payload,
                "fingerprint": fingerprint,
            },
            db_pool=db_pool,
            deps=deps,
            chat_id=chat_id,
        )

        # Batch intake renders one compact summary after all actions are processed.
        if ".batch" not in source:
            from bot.services.freeform_intake import _rerender_with_toast

            await _rerender_with_toast(
                message,
                db_pool,
                deps,
                f"{result}\n↩️ Можно отменить кнопкой «Отмена» или сообщением «отмени».",
            )
        return int(pending_action_id)
    except Exception as exc:
        logger.exception(
            "instant capture failed; falling back to confirmation",
            extra={"kind": kind, "source": source},
        )
        if pending_action_id:
            try:
                async with db_pool.acquire() as conn:
                    await mark_pending_action_status(
                        conn,
                        pending_action_id=int(pending_action_id),
                        status="failed",
                        last_error=str(exc),
                    )
                    await forget_recent_action(
                        conn,
                        chat_id=chat_id,
                        fingerprint=fingerprint,
                    )
            except Exception:
                logger.exception("failed to clean up instant capture state")

        return await original(
            message,
            db_pool=db_pool,
            deps=deps,
            kind=kind,
            payload=payload,
            fingerprint=fingerprint,
            summary=summary,
            source=source,
            force_new=force_new,
        )


async def _resolve_project_to_inbox(
    conn: asyncpg.Connection,
    *,
    requested_code: str | None,
    requested_name: str | None,
    raw_text: str,
    current_project_id: int | None,
    projects: list[Any],
) -> tuple[int | None, str | None, str | None]:
    """Use Inbox when the mentioned project cannot be resolved uniquely."""

    original = _original_resolve_project
    if original is None:
        raise RuntimeError("product mode is not installed")

    project_id, project_code, error = await original(
        conn,
        requested_code=requested_code,
        requested_name=requested_name,
        raw_text=raw_text,
        current_project_id=current_project_id,
        projects=projects,
    )
    if project_id is not None:
        return project_id, project_code, None

    inbox = next(
        (
            project
            for project in projects
            if str(getattr(project, "code", "") or "").upper() == "INBOX"
        ),
        None,
    )
    if inbox is not None:
        return int(inbox.id), str(inbox.code), None

    inbox_id = await ensure_inbox_project_id(conn)
    if error:
        logger.info(
            "unresolved project routed to Inbox",
            extra={"requested_code": requested_code, "requested_name": requested_name},
        )
    return int(inbox_id), "INBOX", None


def _resolve_assignee_tolerant(
    *,
    requested_name: str | None,
    raw_text: str,
    team: list[Any],
) -> tuple[int | None, str | None, str | None]:
    """Leave a task unassigned instead of starting a clarification loop."""

    original = _original_resolve_assignee
    if original is None:
        raise RuntimeError("product mode is not installed")

    assignee_id, assignee_name, error = original(
        requested_name=requested_name,
        raw_text=raw_text,
        team=team,
    )
    if error:
        logger.info("unresolved assignee ignored", extra={"requested_name": requested_name})
        return None, None, None
    return assignee_id, assignee_name, None


async def _ensure_habit_state(conn: asyncpg.Connection, chat_id: int) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS assistant_habit_state (
            chat_id BIGINT PRIMARY KEY,
            last_morning_brief_date DATE,
            last_evening_nudge_date DATE,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    await conn.execute(
        """
        INSERT INTO assistant_habit_state(chat_id)
        VALUES($1)
        ON CONFLICT(chat_id) DO NOTHING
        """,
        int(chat_id),
    )


async def _load_habit_snapshot(
    conn: asyncpg.Connection,
    *,
    chat_id: int,
    tz_name: str,
    today: date,
) -> dict[str, Any]:
    await _ensure_habit_state(conn, chat_id)
    state = await conn.fetchrow(
        """
        SELECT last_morning_brief_date, last_evening_nudge_date
        FROM assistant_habit_state
        WHERE chat_id=$1
        """,
        int(chat_id),
    )

    overdue_count = int(
        await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM tasks
            WHERE status NOT IN ('done', 'postponed')
              AND kind != 'super'
              AND deadline IS NOT NULL
              AND deadline < (NOW() AT TIME ZONE 'UTC')
            """
        )
        or 0
    )
    today_count = int(
        await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM tasks
            WHERE status NOT IN ('done', 'postponed')
              AND kind != 'super'
              AND deadline IS NOT NULL
              AND (deadline AT TIME ZONE 'UTC' AT TIME ZONE $1)::date = $2::date
            """,
            tz_name,
            today,
        )
        or 0
    )
    reminder_count = int(
        await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM reminders
            WHERE cancelled_at_utc IS NULL
              AND status IN ('pending', 'retry', 'claimed')
              AND (remind_at AT TIME ZONE 'UTC' AT TIME ZONE $1)::date = $2::date
            """,
            tz_name,
            today,
        )
        or 0
    )

    inbox_id = await conn.fetchval("SELECT id FROM projects WHERE UPPER(code)='INBOX' LIMIT 1")
    inbox_count = 0
    if inbox_id:
        inbox_count = int(
            await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM tasks
                WHERE status != 'done'
                  AND kind != 'super'
                  AND project_id=$1
                """,
                int(inbox_id),
            )
            or 0
        )

    focus = await conn.fetchrow(
        """
        SELECT t.title, p.code, t.deadline
        FROM tasks t
        JOIN projects p ON p.id=t.project_id
        WHERE t.status NOT IN ('done', 'postponed')
          AND t.kind != 'super'
          AND t.deadline IS NOT NULL
          AND (
                t.deadline < (NOW() AT TIME ZONE 'UTC')
                OR (t.deadline AT TIME ZONE 'UTC' AT TIME ZONE $1)::date = $2::date
          )
        ORDER BY t.deadline ASC
        LIMIT 1
        """,
        tz_name,
        today,
    )

    return {
        "last_morning": state["last_morning_brief_date"] if state else None,
        "last_evening": state["last_evening_nudge_date"] if state else None,
        "overdue": overdue_count,
        "today": today_count,
        "reminders": reminder_count,
        "inbox": inbox_count,
        "focus": dict(focus) if focus else None,
    }


async def _mark_habit_sent(
    conn: asyncpg.Connection,
    *,
    chat_id: int,
    column: str,
    sent_date: date,
) -> None:
    if column not in {"last_morning_brief_date", "last_evening_nudge_date"}:
        raise ValueError("unsupported habit state column")
    await conn.execute(
        f"""
        UPDATE assistant_habit_state
        SET {column}=$2::date, updated_at=NOW()
        WHERE chat_id=$1
        """,
        int(chat_id),
        sent_date,
    )


def _focus_line(focus: dict[str, Any] | None, tz: ZoneInfo) -> str | None:
    if not focus:
        return None
    title = str(focus.get("title") or "").strip()
    project = str(focus.get("code") or "INBOX").strip()
    deadline = focus.get("deadline")
    when = ""
    if deadline is not None:
        try:
            if getattr(deadline, "tzinfo", None) is None:
                from datetime import timezone

                deadline = deadline.replace(tzinfo=timezone.utc)
            when = deadline.astimezone(tz).strftime("%H:%M")
        except Exception:
            when = ""
    suffix = f" · {when}" if when else ""
    return f"🎯 Фокус: [{project}] {title}{suffix}"


async def maybe_send_habit_messages(
    pool: asyncpg.Pool,
    *,
    bot,
    chat_id: int,
    tz_name: str,
) -> dict[str, object]:
    if not _env_enabled("ASSISTANT_HABIT_MESSAGES", True):
        return {"enabled": False, "morning": "disabled", "evening": "disabled"}

    resolved_tz_name = resolve_tz_name(tz_name)
    tz = ZoneInfo(resolved_tz_name)
    now_local = datetime.now(tz)
    morning_hour = _env_hour("ASSISTANT_MORNING_BRIEF_HOUR", 8)
    evening_hour = _env_hour("ASSISTANT_EVENING_NUDGE_HOUR", 19)
    if evening_hour <= morning_hour:
        evening_hour = min(23, morning_hour + 8)

    async with pool.acquire() as conn:
        snapshot = await _load_habit_snapshot(
            conn,
            chat_id=int(chat_id),
            tz_name=resolved_tz_name,
            today=now_local.date(),
        )

    result: dict[str, object] = {"enabled": True, "morning": "not_due", "evening": "not_due"}

    if _morning_due(
        now_local,
        last_sent=snapshot["last_morning"],
        morning_hour=morning_hour,
        evening_hour=evening_hour,
    ):
        lines = [
            "☀️ <b>Доброе утро</b>",
            f"Сегодня: <b>{snapshot['today']}</b> задач · <b>{snapshot['reminders']}</b> напоминаний",
            f"Просрочено: <b>{snapshot['overdue']}</b> · Inbox: <b>{snapshot['inbox']}</b>",
        ]
        focus_line = _focus_line(snapshot.get("focus"), tz)
        if focus_line:
            lines.extend(["", focus_line])
        else:
            lines.extend(["", "День пока свободен — можно быстро добавить первый фокус."])

        await bot.send_message(
            chat_id=int(chat_id),
            text="\n".join(lines),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="📅 Открыть сегодня", callback_data="nav:today"),
                        InlineKeyboardButton(text="➕ Добавить", callback_data="nav:add"),
                    ],
                    [
                        InlineKeyboardButton(
                            text=f"📥 Inbox ({snapshot['inbox']})",
                            callback_data="nav:inbox:0",
                        )
                    ],
                ]
            ),
        )
        async with pool.acquire() as conn:
            await _mark_habit_sent(
                conn,
                chat_id=int(chat_id),
                column="last_morning_brief_date",
                sent_date=now_local.date(),
            )
        result["morning"] = "sent"

    if int(snapshot["inbox"] or 0) > 0 and _evening_due(
        now_local,
        last_sent=snapshot["last_evening"],
        evening_hour=evening_hour,
    ):
        await bot.send_message(
            chat_id=int(chat_id),
            text=(
                "🌙 <b>Inbox ждёт разбора</b>\n"
                f"Осталось записей: <b>{snapshot['inbox']}</b>.\n"
                "Можно разобрать их по одной и закончить день с чистой головой."
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🧹 Разобрать Inbox",
                            callback_data="inbox:triage:start",
                        )
                    ]
                ]
            ),
        )
        async with pool.acquire() as conn:
            await _mark_habit_sent(
                conn,
                chat_id=int(chat_id),
                column="last_evening_nudge_date",
                sent_date=now_local.date(),
            )
        result["evening"] = "sent"

    return result


async def _do_tick_with_habits(*args, **kwargs) -> dict[str, object]:
    original = _original_do_tick
    if original is None:
        raise RuntimeError("product mode is not installed")

    result = await original(*args, **kwargs)
    try:
        pool = args[0] if args else kwargs["pool"]
        habit_result = await maybe_send_habit_messages(
            pool,
            bot=kwargs["bot"],
            chat_id=int(kwargs["admin_id"]),
            tz_name=str(kwargs["tz_name"]),
        )
    except Exception as exc:
        logger.exception("habit message tick failed")
        habit_result = {"enabled": True, "error": type(exc).__name__}

    if isinstance(result, dict):
        result = dict(result)
        result["habit_messages"] = habit_result
    return result


def install_product_mode() -> None:
    """Install runtime behavior patches exactly once."""

    global _installed
    global _original_create_pending_preview
    global _original_resolve_project
    global _original_resolve_assignee
    global _original_do_tick

    if _installed:
        return

    import bot.services.freeform_intake as freeform_intake
    import bot.services.tick as tick_module
    import bot.lifecycle as lifecycle_module
    from bot.http import endpoints as endpoints_module

    _original_create_pending_preview = freeform_intake.create_pending_preview
    _original_resolve_project = freeform_intake._resolve_project
    _original_resolve_assignee = freeform_intake._resolve_assignee
    _original_do_tick = tick_module.do_tick

    freeform_intake.create_pending_preview = _instant_create_pending_preview
    freeform_intake._resolve_project = _resolve_project_to_inbox
    freeform_intake._resolve_assignee = _resolve_assignee_tolerant

    tick_module.do_tick = _do_tick_with_habits
    lifecycle_module.do_tick_service = _do_tick_with_habits
    endpoints_module.do_tick_service = _do_tick_with_habits

    _installed = True
