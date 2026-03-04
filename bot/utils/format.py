"""Formatting helpers.

These helpers are intentionally dependency-light so they can be reused from
handlers, UI renderers, and background jobs.
"""

from __future__ import annotations

import html
from datetime import datetime

from bot.utils.datetime import fmt_msk


def h(s: str) -> str:
    """HTML-escape a string for Telegram HTML parse_mode."""
    return html.escape(s or "")


def fmt_task_line_html(title: str, project: str, assignee: str, deadline_dt: datetime | None) -> str:
    """One task item for lists (HTML)."""
    meta: list[str] = []
    if project:
        meta.append(h(project))
    if assignee:
        meta.append(h(assignee))
    if deadline_dt is not None:
        # deadline_dt is stored as naive UTC in DB; format in app timezone.
        meta.append(f"до {h(fmt_msk(deadline_dt))}")
    meta_txt = " • ".join(meta)
    if meta_txt:
        return f"<b>{h(title)}</b>\n<i>{meta_txt}</i>"
    return f"<b>{h(title)}</b>"


def fmt_portfolio_line(code: str, name: str, active: int, overdue: int, is_current: bool) -> str:
    """Format one portfolio row."""
    star = "⭐ " if is_current else ""
    nm = f" — {name}" if name else ""
    return f"{star}{code}{nm} | задач: {active} | 🚨 {overdue}"
