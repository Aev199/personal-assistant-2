"""Datetime helpers.

The monolith historically mixed parsing logic and business logic. This module
keeps parsing isolated and testable.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import os

import dateparser

from bot.tz import resolve_tz_name


_quick_time_re = re.compile(r"\b(\d{1,2})[:.](\d{2})\b")
_quick_rel_re = re.compile(
    r"\b(?:\u0441\u0435\u0433\u043e\u0434\u043d\u044f|\u0437\u0430\u0432\u0442\u0440\u0430|\u043f\u043e\u0441\u043b\u0435\u0437\u0430\u0432\u0442\u0440\u0430)\b",
    re.IGNORECASE,
)
_quick_dur_re = re.compile(r"\b(\d{1,3})\s*(?:\u043c\u0438\u043d(?:\u0443\u0442)?|m)\b", re.IGNORECASE)
_quick_date_re = re.compile(
    r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?(?:\s+(?:\u0432\s*)?(\d{1,2})[:.](\d{2}))?\b",
    re.IGNORECASE,
)
_quick_rel_dt_re = re.compile(
    r"\b(\u0441\u0435\u0433\u043e\u0434\u043d\u044f|\u0437\u0430\u0432\u0442\u0440\u0430|\u043f\u043e\u0441\u043b\u0435\u0437\u0430\u0432\u0442\u0440\u0430)\b(?:\s*(?:\u0432\s*)?(\d{1,2})[:.](\d{2}))?",
    re.IGNORECASE,
)
_quick_relative_day_offsets = {
    "\u0441\u0435\u0433\u043e\u0434\u043d\u044f": 0,
    "\u0437\u0430\u0432\u0442\u0440\u0430": 1,
    "\u043f\u043e\u0441\u043b\u0435\u0437\u0430\u0432\u0442\u0440\u0430": 2,
}
_quick_month_markers = [
    "\u0434\u043d",
    "\u044f\u043d\u0432",
    "\u0444\u0435\u0432",
    "\u043c\u0430\u0440",
    "\u0430\u043f\u0440",
    "\u043c\u0430\u044f",
    "\u0438\u044e\u043d",
    "\u0438\u044e\u043b",
    "\u0430\u0432\u0433",
    "\u0441\u0435\u043d",
    "\u043e\u043a\u0442",
    "\u043d\u043e\u044f",
    "\u0434\u0435\u043a",
    ".",
]



def _safe_zone(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("UTC")


def ensure_tz(dt: datetime, tz_name: str) -> datetime:
    """Ensure datetime is timezone-aware in tz_name.

    dateparser sometimes returns naive datetime even with RETURN_AS_TIMEZONE_AWARE.
    On servers running in UTC this produces a stable ±TZ offset bug.
    """
    tz = _safe_zone(tz_name)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz)
    try:
        return dt.astimezone(tz)
    except Exception:
        return dt


def parse_datetime_ru(text: str, tz_name: str, *, prefer_future: bool = True) -> datetime | None:
    """Parse RU datetime using dateparser and return tz-aware datetime in tz_name."""
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        dt = dateparser.parse(
            raw,
            languages=["ru"],
            settings={
                "TIMEZONE": tz_name,
                "RETURN_AS_TIMEZONE_AWARE": True,
                "PREFER_DATES_FROM": "future" if prefer_future else "current_period",
                "DATE_ORDER": "DMY",
            },
        )
    except Exception:
        return None
    if not dt:
        return None
    return ensure_tz(dt, tz_name)

def quick_parse_datetime_ru(
    text: str,
    tz_name: str,
    *,
    prefer_future: bool = True,
    date_only_time: tuple[int, int] | None = None,
) -> datetime | None:
    """Best-effort datetime parser for Quick Add (RU).

    We intentionally require a strong signal that user provided date/time.
    This avoids accidental parsing of ordinary text.
    """
    raw = (text or "").strip()
    if not raw:
        return None

    # Require a strong signal that user provided date/time.
    # Keep the heuristic conservative: time, relative day, months, dots.
    raw_lower = raw.lower()

    if not (
        _quick_time_re.search(raw)
        or _quick_rel_re.search(raw)
        or any(k in raw_lower for k in _quick_month_markers)
    ):
        return None

    tz = _safe_zone(tz_name)
    now_local = datetime.now(tz)

    rel_match = _quick_rel_dt_re.search(raw)
    if rel_match:
        rel_token = (rel_match.group(1) or "").lower()
        day_shift = _quick_relative_day_offsets.get(rel_token)
        if day_shift is not None:
            if rel_match.group(2) and rel_match.group(3):
                try:
                    hh = int(rel_match.group(2))
                    mm = int(rel_match.group(3))
                except Exception:
                    hh = mm = -1
            elif date_only_time is not None:
                hh, mm = date_only_time
            else:
                return None
            if 0 <= hh <= 23 and 0 <= mm <= 59:
                base = now_local + timedelta(days=day_shift)
                return base.replace(hour=hh, minute=mm, second=0, microsecond=0)

    date_match = _quick_date_re.search(raw)
    if date_match:
        try:
            day = int(date_match.group(1))
            month = int(date_match.group(2))
            year_raw = date_match.group(3)
            if date_match.group(4) and date_match.group(5):
                hh = int(date_match.group(4))
                mm = int(date_match.group(5))
            elif date_only_time is not None:
                hh, mm = date_only_time
            else:
                return None
            if not (0 <= hh <= 23 and 0 <= mm <= 59):
                raise ValueError("invalid time")
            year = now_local.year if not year_raw else int(year_raw)
            if year < 100:
                year += 2000
            dt = datetime(year, month, day, hh, mm, tzinfo=tz)
            if prefer_future and dt < now_local and not year_raw:
                dt = dt.replace(year=dt.year + 1)
            return dt
        except Exception:
            pass

    try:
        dt = dateparser.parse(
            raw,
            languages=["ru"],
            settings={
                "TIMEZONE": tz_name,
                "RETURN_AS_TIMEZONE_AWARE": True,
                "PREFER_DATES_FROM": "future" if prefer_future else "current_period",
                "DATE_ORDER": "DMY",
            },
        )
    except Exception:
        return None

    if not dt:
        return None

    # dateparser may return naive dt even with RETURN_AS_TIMEZONE_AWARE; normalize to tz-aware.
    return ensure_tz(dt, tz_name)


def quick_parse_duration_min(text: str) -> int | None:
    """Extract duration in minutes from a free-form RU string.

    Conservative: requires explicit "мин"/"минут"/"m" marker.
    """
    raw = (text or "").strip()
    if not raw:
        return None
    m = _quick_dur_re.search(raw)
    if not m:
        return None
    try:
        v = int(m.group(1))
    except Exception:
        return None
    if 5 <= v <= 600:
        return v
    return None


def fmt_msk(dt: datetime | None) -> str:
    """Format a datetime in app timezone (defaults to TZ env / Europe/Moscow)."""
    if dt is None:
        return "—"
    tz_name = resolve_tz_name("Europe/Moscow")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Europe/Moscow")
    try:
        if dt.tzinfo is None:
            # Treat naive as UTC for backward compatibility
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
        return dt.astimezone(tz).strftime("%d.%m %H:%M")
    except Exception:
        return "—"
