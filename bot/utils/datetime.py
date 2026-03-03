"""Datetime helpers.

The monolith historically mixed parsing logic and business logic. This module
keeps parsing isolated and testable.
"""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo
import os

import dateparser


_quick_time_re = re.compile(r"\b(\d{1,2})[:.](\d{2})\b")
_quick_rel_re = re.compile(r"\b(сегодня|завтра|послезавтра)\b", re.IGNORECASE)
_quick_dur_re = re.compile(r"\b(\d{1,3})\s*(?:мин(?:ут)?|m)\b", re.IGNORECASE)


def quick_parse_datetime_ru(text: str, tz_name: str, *, prefer_future: bool = True) -> datetime | None:
    """Best-effort datetime parser for Quick Add (RU).

    We intentionally require a strong signal that user provided date/time.
    This avoids accidental parsing of ordinary text.
    """
    raw = (text or "").strip()
    if not raw:
        return None

    # Require a strong signal that user provided date/time.
    # Keep the heuristic conservative: time, relative day, months, dots.
    if not (
        _quick_time_re.search(raw)
        or _quick_rel_re.search(raw)
        or any(
            k in raw.lower()
            for k in [
                "дн",
                "янв",
                "фев",
                "мар",
                "апр",
                "мая",
                "июн",
                "июл",
                "авг",
                "сен",
                "окт",
                "ноя",
                "дек",
                ".",
            ]
        )
    ):
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

    # dateparser may return naive dt depending on settings; normalize to aware if possible.
    if dt.tzinfo is None:
        # Keep as naive; caller decides how to store.
        return dt
    return dt


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
    tz_name = os.getenv("TZ") or "Europe/Moscow"
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
