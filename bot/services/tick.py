"""Cron tick service for reminders and background retries."""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable

import asyncpg
from aiogram import Bot

from bot.adapters.icloud_caldav_adapter import ICloudCalDAVAdapter
from bot.services.icloud_retry import retry_pending_icloud_events
from bot.services.logger import StructuredLogger, get_logger
from bot.services.reminders import next_repeat_time_utc_naive, send_reminder
from bot.tz import to_db_utc


async def _claim_due_reminders(
    conn: asyncpg.Connection,
    *,
    batch_limit: int,
    fallback_chat_id: int,
) -> list[asyncpg.Record]:
    claim_token = str(uuid.uuid4())
    rows = await conn.fetch(
        """
        WITH due AS (
            SELECT id
            FROM reminders
            WHERE cancelled_at_utc IS NULL
              AND chat_id IS NOT NULL
              AND (
                    status IN ('pending', 'retry')
                    OR (status = 'claimed' AND claimed_at_utc < NOW() - INTERVAL '10 minutes')
              )
              AND COALESCE(next_attempt_at_utc, remind_at AT TIME ZONE 'UTC') <= NOW()
            ORDER BY COALESCE(next_attempt_at_utc, remind_at AT TIME ZONE 'UTC') ASC, id ASC
            FOR UPDATE SKIP LOCKED
            LIMIT $1
        )
        UPDATE reminders r
        SET status='claimed',
            claim_token=$2::uuid,
            claimed_at_utc=NOW(),
            last_attempt_at_utc=NOW(),
            attempt_count=COALESCE(r.attempt_count, 0) + 1,
            error_code=NULL,
            next_attempt_at_utc=COALESCE(r.next_attempt_at_utc, r.remind_at AT TIME ZONE 'UTC')
        FROM due
        WHERE r.id = due.id
        RETURNING r.id,
                  COALESCE(r.chat_id, $3::bigint) AS chat_id,
                  r.text,
                  r.remind_at,
                  COALESCE(r.repeat, 'none') AS repeat,
                  r.claim_token,
                  r.attempt_count
        """,
        int(batch_limit),
        claim_token,
        int(fallback_chat_id),
    )
    return list(rows or [])


def _retry_delay_sec(attempt_count: int) -> int:
    schedule = [60, 300, 900, 1800, 3600, 7200]
    idx = min(max(0, int(attempt_count) - 1), len(schedule) - 1)
    return schedule[idx]


async def _ack_sent(
    conn: asyncpg.Connection,
    *,
    reminder_id: int,
    claim_token: str,
    repeat: str,
    remind_at,
    tz_name: str,
) -> None:
    rep = (repeat or "none").strip().lower()
    if rep != "none":
        nxt = next_repeat_time_utc_naive(remind_at, rep, tz_name=tz_name)
        if nxt is not None:
            next_attempt_at = nxt.replace(tzinfo=timezone.utc)
            nxt_db = to_db_utc(
                next_attempt_at,
                tz_name=tz_name,
                store_tz=False,
            )
            await conn.execute(
                """
                UPDATE reminders
                SET status='pending',
                    next_attempt_at_utc=$3,
                    remind_at=$4,
                    sent_at_utc=NOW(),
                    claimed_at_utc=NULL,
                    claim_token=NULL,
                    is_sent=FALSE,
                    error_code=NULL
                WHERE id=$1 AND claim_token=$2::uuid
                """,
                int(reminder_id),
                str(claim_token),
                next_attempt_at,
                nxt_db,
            )
            return

    await conn.execute(
        """
        UPDATE reminders
        SET status='sent',
            sent_at_utc=NOW(),
            claimed_at_utc=NULL,
            claim_token=NULL,
            is_sent=TRUE,
            error_code=NULL
        WHERE id=$1 AND claim_token=$2::uuid
        """,
        int(reminder_id),
        str(claim_token),
    )


async def _ack_failed(
    conn: asyncpg.Connection,
    *,
    reminder_id: int,
    claim_token: str,
    attempt_count: int,
    max_attempts: int,
    error_code: str,
) -> str:
    if int(attempt_count) >= int(max_attempts):
        await conn.execute(
            """
            UPDATE reminders
            SET status='failed',
                claimed_at_utc=NULL,
                claim_token=NULL,
                error_code=$3
            WHERE id=$1 AND claim_token=$2::uuid
            """,
            int(reminder_id),
            str(claim_token),
            str(error_code),
        )
        return "failed"

    retry_at = datetime.now(timezone.utc) + timedelta(seconds=_retry_delay_sec(int(attempt_count)))
    await conn.execute(
        """
        UPDATE reminders
        SET status='retry',
            next_attempt_at_utc=$3,
            claimed_at_utc=NULL,
            claim_token=NULL,
            error_code=$4,
            is_sent=FALSE
        WHERE id=$1 AND claim_token=$2::uuid
        """,
        int(reminder_id),
        str(claim_token),
        retry_at,
        str(error_code),
    )
    return "retry"


async def do_tick(
    pool: asyncpg.Pool,
    *,
    bot: Bot,
    admin_id: int,
    tz_name: str,
    send_timeout_sec: float,
    icloud: ICloudCalDAVAdapter | None = None,
    icloud_enabled: bool = False,
    error_logger: Callable[[str, BaseException, dict | None], "object"] | None = None,
    logger: StructuredLogger | None = None,
    batch_limit: int = 50,
    time_budget_sec: float = 20.0,
    max_attempts: int = 6,
) -> dict[str, object]:
    """Run one cron tick cycle and return structured counters."""

    log = logger or get_logger("bot.tick")
    started = time.monotonic()
    delivered = 0
    retried = 0
    failed = 0
    claimed_total = 0
    batches = 0

    while (time.monotonic() - started) < max(1.0, float(time_budget_sec)):
        async with pool.acquire() as conn:
            async with conn.transaction():
                records = await _claim_due_reminders(
                    conn,
                    batch_limit=int(batch_limit),
                    fallback_chat_id=int(admin_id),
                )
        if not records:
            break

        batches += 1
        claimed_total += len(records)

        for record in records:
            reminder_id = int(record["id"])
            claim_token = str(record["claim_token"])
            ok = await send_reminder(
                bot=bot,
                chat_id=int(record["chat_id"] or admin_id),
                reminder_id=reminder_id,
                text=str(record["text"] or ""),
                send_timeout_sec=send_timeout_sec,
                action_token=claim_token,
            )
            async with pool.acquire() as conn:
                if ok:
                    await _ack_sent(
                        conn,
                        reminder_id=reminder_id,
                        claim_token=claim_token,
                        repeat=str(record["repeat"] or "none"),
                        remind_at=record["remind_at"],
                        tz_name=tz_name,
                    )
                    delivered += 1
                else:
                    new_status = await _ack_failed(
                        conn,
                        reminder_id=reminder_id,
                        claim_token=claim_token,
                        attempt_count=int(record["attempt_count"] or 1),
                        max_attempts=int(max_attempts),
                        error_code="telegram_send_failed",
                    )
                    if new_status == "failed":
                        failed += 1
                    else:
                        retried += 1

        if len(records) < int(batch_limit):
            break

    icloud_result: str = "skipped"
    if icloud_enabled and icloud is not None:
        try:
            await retry_pending_icloud_events(pool, icloud, error_logger=error_logger)
            icloud_result = "ok"
        except Exception as e:
            icloud_result = "failed"
            log.warning(
                "iCloud retry failed",
                error_type=type(e).__name__,
                error_message=str(e),
            )
            if error_logger:
                try:
                    await error_logger("tick.icloud_retry", e, None)
                except Exception:
                    pass

    async with pool.acquire() as conn:
        # Cleanup expired pending action previews
        expired_rows = await conn.fetch(
            """
            DELETE FROM pending_actions
            WHERE status = 'pending' AND expires_at < NOW()
            RETURNING chat_id, source_message_id
            """
        )
        expired_cleaned = 0
        if expired_rows:
            for row in expired_rows:
                if row["source_message_id"]:
                    try:
                        await bot.delete_message(chat_id=int(row["chat_id"]), message_id=int(row["source_message_id"]))
                        expired_cleaned += 1
                    except Exception:
                        pass
        
        due_backlog = int(
            await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM reminders
                WHERE status IN ('pending', 'retry')
                  AND cancelled_at_utc IS NULL
                  AND COALESCE(next_attempt_at_utc, remind_at AT TIME ZONE 'UTC') <= NOW()
                """
            )
            or 0
        )

    return {
        "ok": True,
        "claimed": claimed_total,
        "delivered": delivered,
        "retried": retried,
        "failed": failed,
        "batches": batches,
        "due_backlog": due_backlog,
        "icloud": icloud_result,
        "expired_pending_cleaned": expired_cleaned,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }
