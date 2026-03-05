"""Telegram-specific helpers."""

from __future__ import annotations

import html
import re

from aiogram.types import Message


TG_TEXT_LIMIT_UNITS = 4096
TG_TEXT_SAFE_MARGIN_UNITS = 64
TG_TEXT_SAFE_LIMIT_UNITS = TG_TEXT_LIMIT_UNITS - TG_TEXT_SAFE_MARGIN_UNITS


def _tg_utf16_units(text: str) -> int:
    """Telegram counts message length in UTF-16 code units."""
    if not text:
        return 0
    return len(text.encode("utf-16-le")) // 2


def _trim_to_units(text: str, max_units: int) -> str:
    if max_units <= 0 or not text:
        return ""
    if _tg_utf16_units(text) <= max_units:
        return text
    lo = 0
    hi = len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _tg_utf16_units(text[:mid]) <= max_units:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo]


def fit_telegram_text(text: str, *, parse_mode: str | None = None, max_units: int = TG_TEXT_SAFE_LIMIT_UNITS) -> str:
    """Trim text to Telegram limits (by UTF-16 units), preserving line boundaries."""
    text = str(text or "")
    try:
        max_units = int(max_units)
    except Exception:
        max_units = TG_TEXT_SAFE_LIMIT_UNITS

    if _tg_utf16_units(text) <= max_units:
        return text

    is_html = bool((parse_mode or "").upper() == "HTML")
    lines = text.splitlines()
    suffix_tpl = "\n\n<i>… и ещё {n} строк</i>" if is_html else "\n\n… и ещё {n} строк"

    total = len(lines)
    for keep in range(max(1, total - 1), 0, -1):
        hidden = total - keep
        if hidden <= 0:
            continue
        candidate = "\n".join(lines[:keep]) + suffix_tpl.format(n=hidden)
        if _tg_utf16_units(candidate) <= max_units:
            return candidate

    plain = re.sub(r"<[^>]+>", "", lines[0] if lines else text)
    plain = plain.strip() or "…"
    if is_html:
        plain = html.escape(plain)
    cut = _trim_to_units(plain, max(1, max_units - 1))
    return cut + "…"


async def try_delete_user_message(message: Message) -> None:
    """Best-effort delete user's message to avoid chat spam."""
    try:
        await message.delete()
    except Exception:
        return
