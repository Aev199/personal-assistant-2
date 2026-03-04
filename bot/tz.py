"""Timezone resolution helpers.

Why this exists:
- Hosting providers (Render/Heroku/etc.) often set TZ=UTC at the process level.
- In this project, deadlines/reminders are stored as *naive UTC* timestamps in DB.
  For correct UX, we must always know the *application* timezone used for UI.

We therefore prefer an explicit app timezone env var and only fall back to TZ
when it looks intentionally set by the user.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


_UTC_NAMES = {"UTC", "ETC/UTC", "GMT", "ETC/GMT"}


def resolve_tz_name(default: str = "Europe/Moscow") -> str:
    """Resolve application timezone name.

    Priority:
      1) BOT_TIMEZONE / APP_TIMEZONE / BOT_TZ
      2) TZ (only if it doesn't look like a provider default like UTC)
      3) default
    """

    for k in ("BOT_TIMEZONE", "APP_TIMEZONE", "BOT_TZ"):
        v = (os.getenv(k) or "").strip()
        if v:
            return v

    tz = (os.getenv("TZ") or "").strip()
    if tz and tz.upper() not in _UTC_NAMES:
        return tz

    return default


def resolve_tzinfo(default: str = "Europe/Moscow"):
    """Resolve application tzinfo.

    Defensive approach:
    - Prefer explicit app env vars (via :func:`resolve_tz_name`).
    - If ZoneInfo is unavailable for the requested name (missing tzdata),
      fall back to the *system local timezone* when it's non-UTC.
    - Otherwise fall back to UTC.
    """

    name = resolve_tz_name(default)
    try:
        return ZoneInfo(name)
    except Exception:
        sys_tz = datetime.now().astimezone().tzinfo
        try:
            if sys_tz is not None:
                off = sys_tz.utcoffset(datetime.now())
                if off and off.total_seconds() != 0:
                    return sys_tz
        except Exception:
            pass
        return timezone.utc


def to_utc_aware(dt: datetime | None) -> datetime | None:
    """Normalize datetime to timezone-aware UTC.

    Project convention:
    - DB stores deadlines/reminders as *naive UTC* timestamps (TIMESTAMP without TZ).
    - For display and scheduling we always treat naive values as UTC.
    """

    if dt is None:
        return None
    if getattr(dt, "tzinfo", None) is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_local(dt_utc_naive: datetime | None, tzinfo=None, *, default: str = "Europe/Moscow") -> datetime | None:
    """Convert a UTC-naive (or any aware) datetime to local tzinfo."""

    d = to_utc_aware(dt_utc_naive)
    if d is None:
        return None
    tzinfo = tzinfo or resolve_tzinfo(default)
    try:
        return d.astimezone(tzinfo)
    except Exception:
        return d


def fmt_local(dt_utc_naive: datetime | None, tzinfo=None, *, default: str = "Europe/Moscow", fmt: str = "%d.%m %H:%M") -> str:
    d = to_local(dt_utc_naive, tzinfo, default=default)
    return d.strftime(fmt) if d else "—"


def to_db_utc(dt_local: datetime | None, *, tz_name: str, store_tz: bool) -> datetime | None:
    """Convert a local datetime to the DB storage representation.

    The project historically stored UTC-naive TIMESTAMP values in DB.
    Some deployments have TIMESTAMPTZ columns; for those we store UTC-aware
    datetimes to avoid session-timezone casts.
    """

    if dt_local is None:
        return None

    if getattr(dt_local, "tzinfo", None) is None:
        try:
            dt_local = dt_local.replace(tzinfo=ZoneInfo(tz_name))
        except Exception:
            dt_local = dt_local.replace(tzinfo=timezone.utc)

    utc = dt_local.astimezone(timezone.utc)
    return utc if store_tz else utc.replace(tzinfo=None)
