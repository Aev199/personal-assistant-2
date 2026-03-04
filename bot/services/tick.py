"""Cron tick service.

The /tick endpoint is called periodically by an external cron. It is
responsible for:
  - delivering due reminders (Telegram)
  - scheduling repeating reminders
  - retrying pending iCloud events

This module keeps tick logic out of the handler monolith.
"""

from __future__ import annotations

from typing import Callable

from datetime import timezone

import asyncpg
from aiogram import Bot

from bot.adapters.icloud_caldav_adapter import ICloudCalDAVAdapter
from bot.services.icloud_retry import retry_pending_icloud_events
from bot.services.logger import StructuredLogger, get_logger
from bot.services.reminders import next_repeat_time_utc_naive, send_reminder
from bot.tz import to_db_utc


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
) -> None:
    """Run one cron tick cycle."""

    log = logger or get_logger("bot.tick")

    # Step 1: fetch due reminders WITHOUT holding conn while doing network IO
    async with pool.acquire() as conn:
        # Support both TIMESTAMP (UTC-naive) and TIMESTAMPTZ schemas.
        remind_dtype = None
        try:
            remind_dtype = await conn.fetchval(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='reminders' AND column_name='remind_at'"
            )
        except Exception:
            remind_dtype = None
        remind_is_timestamptz = (remind_dtype == 'timestamp with time zone')
        now_expr = "NOW()" if remind_is_timestamptz else "(now() AT TIME ZONE 'UTC')"
        records = await conn.fetch(
            "SELECT id, text, remind_at, COALESCE(repeat,'none') AS repeat "
            "FROM reminders "
            f"WHERE remind_at <= {now_expr} AND is_sent = FALSE "
            "ORDER BY remind_at ASC "
            "LIMIT 50"
        )

    if not records:
        # Still retry iCloud events even when there are no reminders.
        if icloud_enabled and icloud is not None:
            await retry_pending_icloud_events(pool, icloud, error_logger=error_logger)
        return

    mark_sent_ids: list[int] = []
    repeat_updates: list[tuple[int, object]] = []  # (id, next_remind_at)

    for r in records:
        rid = int(r["id"])
        text = r["text"]

        ok = await send_reminder(
            bot=bot,
            chat_id=admin_id,
            reminder_id=rid,
            text=text,
            send_timeout_sec=send_timeout_sec,
        )
        if not ok:
            continue

        rep = (r.get("repeat") or "none").strip().lower()
        if rep != "none":
            nxt = next_repeat_time_utc_naive(r["remind_at"], rep, tz_name=tz_name)
            if nxt is None:
                mark_sent_ids.append(rid)
            else:
                # `nxt` is UTC-naive by convention; adapt to actual column type.
                nxt_db = to_db_utc(
                    nxt.replace(tzinfo=timezone.utc),
                    tz_name=tz_name,
                    store_tz=bool(remind_is_timestamptz),
                )
                repeat_updates.append((rid, nxt_db))
        else:
            mark_sent_ids.append(rid)

    # Step 2: update DB (batch)
    async with pool.acquire() as conn:
        if mark_sent_ids:
            await conn.execute(
                "UPDATE reminders SET is_sent = TRUE WHERE id = ANY($1::int[])",
                mark_sent_ids,
            )
        for rid, nxt in repeat_updates:
            await conn.execute(
                "UPDATE reminders SET remind_at = $2, is_sent = FALSE WHERE id = $1",
                rid,
                nxt,
            )

    # Step 3: retry pending iCloud events
    if icloud_enabled and icloud is not None:
        try:
            await retry_pending_icloud_events(pool, icloud, error_logger=error_logger)
        except Exception as e:
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
