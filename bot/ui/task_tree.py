"""Task tree rendering for Telegram UI."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

UTC = ZoneInfo("UTC")


def to_utc(dt):
    """Normalize datetime to timezone-aware UTC. Treat naive as UTC."""
    if dt is None:
        return None
    if getattr(dt, "tzinfo", None) is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def render_task_tree(tasks: list[dict], tz: ZoneInfo) -> tuple[str, list[int]]:
    """Render nested tasks as plain text with indentation.

    Returns:
        text, stable pre-order ids
    """

    task_by_id = {t["id"]: t for t in tasks}
    children: dict[int | None, list[int]] = {}
    for t in tasks:
        pid = t.get("parent_task_id")
        children.setdefault(pid, []).append(t["id"])

    now_utc = datetime.now(UTC)

    def sort_key(tid: int):
        t = task_by_id[tid]
        dl = t.get("deadline")
        if dl is None:
            return (1, datetime.max.replace(tzinfo=UTC), tid)
        dl_utc = to_utc(dl)
        return (0, dl_utc, tid)

    for pid in list(children.keys()):
        children[pid].sort(key=sort_key)

    roots: list[int] = []
    for tid, t in task_by_id.items():
        pid = t.get("parent_task_id")
        if pid is None or pid not in task_by_id:
            roots.append(tid)
    roots = sorted(set(roots), key=sort_key)

    lines: list[str] = []
    order: list[int] = []
    visited: set[int] = set()

    def status_icon(st: str) -> str:
        st = (st or "todo").lower()
        if st == "done":
            return "✅"
        if st == "in_progress":
            return "⏳"
        if st in {"postponed", "blocked"}:
            return "⏸"
        return "•"

    def walk(tid: int, depth: int):
        if tid in visited:
            return
        visited.add(tid)
        t = task_by_id[tid]

        dl = t.get("deadline")
        dl_txt = ""
        overdue = False
        if dl:
            dl_utc = to_utc(dl)
            overdue = dl_utc < now_utc and (t.get("status") not in {"done"})
            dl_local = dl_utc.astimezone(tz)
            dl_txt = f" (до {dl_local.strftime('%d.%m %H:%M')})"

        assignee = t.get("assignee") or "—"
        icon = status_icon(t.get("status", "todo"))
        suffix_parts = []
        if overdue:
            suffix_parts.append("🚨")
        suffix_parts.append(icon)
        suffix = "  " + " ".join(suffix_parts) if suffix_parts else ""
        prefix = ("\xa0\xa0\xa0\xa0" * depth) + ("↳ " if depth > 0 else "")
        lines.append(f"{prefix}{assignee}: {t['title']}{dl_txt}{suffix}")
        order.append(tid)
        for cid in children.get(tid, []):
            walk(cid, depth + 1)

    for rid in roots:
        walk(rid, 0)

    return "\n".join(lines), order
