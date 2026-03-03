"""Text and keyboard layout helpers."""

from __future__ import annotations

import re
from aiogram.types import InlineKeyboardButton


def canon(text: str) -> str:
    """Canonicalize user/button text: strip leading emojis/punctuation and normalize case."""
    if not text:
        return ""
    # Remove leading non-word chars (emoji, punctuation, spaces)
    text = re.sub(r"^[\W_]+", "", text, flags=re.UNICODE)
    return text.strip().lower()


def kb_columns(buttons: list[InlineKeyboardButton], cols: int = 2) -> list[list[InlineKeyboardButton]]:
    """Split a flat list of inline buttons into rows with a fixed number of columns."""
    cols = max(1, int(cols or 1))
    rows: list[list[InlineKeyboardButton]] = []
    cur: list[InlineKeyboardButton] = []
    for b in buttons:
        cur.append(b)
        if len(cur) >= cols:
            rows.append(cur)
            cur = []
    if cur:
        rows.append(cur)
    return rows
