"""Retry logic for pending iCloud events.

Event creation is "best effort": when iCloud is unavailable the bot stores
pending events in the DB and a cron-driven /tick endpoint retries them.
"""

from __future__ import annotations

from typing import Callable

import asyncpg

from bot.adapters.icloud_caldav_adapter import ICloudCalDAVAdapter
from bot.services.logger import get_logger


log = get_logger("bot.services.icloud_retry")


async def retry_pending_icloud_events(
    pool: asyncpg.Pool,
    icloud: ICloudCalDAVAdapter,
    *,
    batch_limit: int = 10,
    min_retry_interval_min: int = 15,
    max_retries: int = 3,
    error_logger: Callable[[str, BaseException, dict | None], "asyncio.Future"] | None = None,
) -> None:
    """Retry pending iCloud events.

    - Retries events with sync_status='pending'
    - Uses last_retry_at to avoid hammering iCloud
    - Marks events as sync_failed after max_retries
    """

    try:
        async with pool.acquire() as conn:
            events = await conn.fetch(
                f"""
                SELECT id, calendar_url, summary, dtstart_utc, dtend_utc,
                       description, location, retry_count
                FROM icloud_events
                WHERE sync_status = 'pending'
                  AND retry_count < {int(max_retries)}
                  AND (last_retry_at IS NULL OR last_retry_at < NOW() - INTERVAL '{int(min_retry_interval_min)} minutes')
                ORDER BY id ASC
                LIMIT {int(batch_limit)}
                """
            )
    except Exception as e:
        log.warning(
            "failed to fetch pending icloud events",
            error_type=type(e).__name__,
            error_message=str(e),
        )
        if error_logger:
            try:
                await error_logger("icloud_retry.fetch", e, None)
            except Exception:
                pass
        return

    if not events:
        return

    for event in events:
        event_id = int(event["id"])
        retry_count = int(event.get("retry_count") or 0)

        try:
            ics_url, success = await icloud.create_event(
                calendar_url=event["calendar_url"],
                summary=event["summary"],
                dtstart_utc=event["dtstart_utc"],
                dtend_utc=event["dtend_utc"],
                description=event.get("description") or "",
                location=event.get("location") or "",
            )

            async with pool.acquire() as conn:
                if success:
                    await conn.execute(
                        """
                        UPDATE icloud_events
                        SET sync_status='synced', ics_url=$2, last_retry_at=NOW(), last_error=NULL
                        WHERE id=$1
                        """,
                        event_id,
                        ics_url,
                    )
                    log.info("icloud event synced", event_id=event_id, retry_count=retry_count)
                else:
                    new_retry_count = retry_count + 1
                    if new_retry_count >= max_retries:
                        await conn.execute(
                            """
                            UPDATE icloud_events
                            SET sync_status='sync_failed', retry_count=$2, last_retry_at=NOW(), last_error=$3
                            WHERE id=$1
                            """,
                            event_id,
                            new_retry_count,
                            "Failed after retries",
                        )
                        log.warning("icloud event marked sync_failed", event_id=event_id)
                    else:
                        await conn.execute(
                            """
                            UPDATE icloud_events
                            SET retry_count=$2, last_retry_at=NOW(), last_error=$3
                            WHERE id=$1
                            """,
                            event_id,
                            new_retry_count,
                            "Sync failed, will retry",
                        )

        except Exception as e:
            new_retry_count = retry_count + 1
            msg = str(e)[:500]
            try:
                async with pool.acquire() as conn:
                    if new_retry_count >= max_retries:
                        await conn.execute(
                            """
                            UPDATE icloud_events
                            SET sync_status='sync_failed', retry_count=$2, last_retry_at=NOW(), last_error=$3
                            WHERE id=$1
                            """,
                            event_id,
                            new_retry_count,
                            msg,
                        )
                    else:
                        await conn.execute(
                            """
                            UPDATE icloud_events
                            SET retry_count=$2, last_retry_at=NOW(), last_error=$3
                            WHERE id=$1
                            """,
                            event_id,
                            new_retry_count,
                            msg,
                        )
            except Exception:
                pass

            log.warning(
                "icloud event retry failed",
                event_id=event_id,
                retry_count=new_retry_count,
                error_message=msg,
            )
            if error_logger:
                try:
                    await error_logger("icloud_retry.create", e, {"event_id": event_id})
                except Exception:
                    pass
