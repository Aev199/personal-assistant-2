"""Calendar snapshot for attention surfaces.

This module keeps CalDAV/network concerns out of native clients and adds a short
in-process cache so widgets do not turn every timeline refresh into several
calendar round-trips.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from bot.adapters.icloud_caldav_adapter import ICloudCalDAVAdapter, ICloudVisibleEvent


logger = logging.getLogger(__name__)
UTC = timezone.utc


@dataclass(frozen=True)
class TodayCalendarSnapshot:
    events: tuple[ICloudVisibleEvent, ...]
    unavailable: bool = False
    pending: bool = False


_CACHE: dict[tuple[int, str, str, tuple[str, ...]], tuple[float, TodayCalendarSnapshot]] = {}


def _dedupe_events(events: list[ICloudVisibleEvent]) -> tuple[ICloudVisibleEvent, ...]:
    """Prefer UID identity, with summary/start as a safe fallback."""
    by_uid: dict[str, ICloudVisibleEvent] = {}
    by_key: dict[tuple[str, datetime], ICloudVisibleEvent] = {}

    for event in events:
        if event.uid:
            key = event.uid.strip()
            existing = by_uid.get(key)
            if existing is None or (event.dtend_utc - event.dtstart_utc) > (
                existing.dtend_utc - existing.dtstart_utc
            ):
                by_uid[key] = event
            continue

        key = ((event.summary or "").strip().lower(), event.dtstart_utc)
        existing = by_key.get(key)
        if existing is None or (event.dtend_utc - event.dtstart_utc) > (
            existing.dtend_utc - existing.dtstart_utc
        ):
            by_key[key] = event

    # A duplicated event may still carry different UIDs in two subscribed
    # calendars. Collapse that last case by title + start.
    final: dict[tuple[str, datetime], ICloudVisibleEvent] = {}
    for event in [*by_uid.values(), *by_key.values()]:
        key = ((event.summary or "").strip().lower()[:120], event.dtstart_utc)
        existing = final.get(key)
        if existing is None or (event.dtend_utc - event.dtstart_utc) > (
            existing.dtend_utc - existing.dtstart_utc
        ):
            final[key] = event

    return tuple(
        sorted(
            final.values(),
            key=lambda item: (item.dtstart_utc, item.dtend_utc, item.summary.lower()),
        )
    )


async def fetch_today_calendar(
    *,
    tz: ZoneInfo,
    calendar_urls: list[str],
    icloud: ICloudCalDAVAdapter | None,
    cache_ttl_sec: float = 90.0,
) -> TodayCalendarSnapshot:
    urls = tuple(url for url in (str(value or "").strip() for value in calendar_urls) if url)
    if not urls:
        return TodayCalendarSnapshot(events=())
    if icloud is None:
        return TodayCalendarSnapshot(events=(), unavailable=True)

    now_local = datetime.now(tz)
    key = (id(icloud), getattr(tz, "key", str(tz)), now_local.date().isoformat(), urls)
    cached = _CACHE.get(key)
    now_mono = time.monotonic()
    if cached and cached[0] > now_mono:
        return cached[1]

    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)
    start_utc = start_local.astimezone(UTC)
    end_utc = end_local.astimezone(UTC)

    results = await asyncio.gather(
        *[
            icloud.list_events(url, start_utc=start_utc, end_utc=end_utc)
            for url in urls
        ],
        return_exceptions=True,
    )

    unavailable = False
    all_events: list[ICloudVisibleEvent] = []
    for result in results:
        if isinstance(result, Exception):
            unavailable = True
            logger.warning("today calendar fetch failed: %s", result)
            continue
        all_events.extend(result)

    snapshot = TodayCalendarSnapshot(
        events=_dedupe_events(all_events),
        unavailable=unavailable,
    )
    _CACHE[key] = (now_mono + max(10.0, float(cache_ttl_sec)), snapshot)

    # Do not retain stale days/configurations indefinitely.
    if len(_CACHE) > 12:
        expired = [cache_key for cache_key, item in _CACHE.items() if item[0] <= now_mono]
        for cache_key in expired:
            _CACHE.pop(cache_key, None)

    return snapshot
