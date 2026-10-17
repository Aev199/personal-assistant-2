"""«На сегодня»: one short list of today's tasks shared by every client.

A task is on today's list when the user planned it for today, or when its
deadline falls on today and the user has not removed it from today's list.
Nothing disappears overnight: yesterday's unfinished plan simply stops being
today's list and the task stays in the backlog.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from bot.db import db_add_event
from bot.tz import resolve_tz_name


class DayPlanError(Exception):
    def __init__(self, code: str, status: int):
        self.code, self.status = code, status
        super().__init__(code)


def local_today(tz_name: str | None, *, now: datetime | None = None) -> date:
    tz = ZoneInfo(resolve_tz_name(tz_name))
    return (now or datetime.now(timezone.utc)).astimezone(tz).date()


def today_plan_sql(day_param: str, tz_param: str, alias: str = "t") -> str:
    """SQL predicate; deadlines are stored as UTC-naive timestamps."""
    # COALESCE keeps the predicate strictly true/false, so NOT(...) also works.
    return f"""(
        COALESCE({alias}.planned_for = {day_param}::date, FALSE)
        OR (
            {alias}.deadline IS NOT NULL
            AND ({alias}.deadline AT TIME ZONE 'UTC' AT TIME ZONE {tz_param})::date = {day_param}::date
            AND {alias}.plan_dismissed_for IS DISTINCT FROM {day_param}::date
        )
    )"""


def in_today_plan(row, *, today: date, tz: ZoneInfo) -> bool:
    if row.get("planned_for") == today:
        return True
    deadline = row.get("deadline")
    if deadline is None:
        return False
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    return deadline.astimezone(tz).date() == today and row.get("plan_dismissed_for") != today


async def set_planned(conn, *, task_id: int, planned: bool, today: date) -> dict:
    """Caller owns the transaction."""
    row = await conn.fetchrow(
        "SELECT id, title, status, kind, project_id FROM tasks WHERE id=$1 FOR UPDATE",
        int(task_id),
    )
    if not row:
        raise DayPlanError("task_not_found", 404)
    if row["kind"] == "super" or row["status"] in ("done", "postponed"):
        raise DayPlanError("task_not_active", 409)
    if planned:
        await conn.execute(
            "UPDATE tasks SET planned_for=$2, plan_dismissed_for=NULL WHERE id=$1",
            int(task_id),
            today,
        )
    else:
        await conn.execute(
            "UPDATE tasks SET planned_for=NULL, plan_dismissed_for=$2 WHERE id=$1",
            int(task_id),
            today,
        )
    await db_add_event(
        conn,
        "task_planned" if planned else "task_unplanned",
        int(row["project_id"]),
        int(task_id),
        f"today plan {today.isoformat()}",
    )
    return {"ok": True, "task_id": int(task_id), "status": "planned" if planned else "unplanned"}


_ACTIVE = """t.status NOT IN ('done','postponed') AND t.kind != 'super'
             AND p.status IN ('active','system')"""


async def load_plan(conn, *, tz_name: str, today: date) -> list[dict]:
    rows = await conn.fetch(
        f"""
        SELECT t.id, t.title, t.deadline, t.kind, p.code AS project
        FROM tasks t JOIN projects p ON p.id=t.project_id
        WHERE {_ACTIVE} AND {today_plan_sql('$1', '$2')}
        ORDER BY t.deadline ASC NULLS LAST, t.id ASC
        LIMIT 30
        """,
        today,
        resolve_tz_name(tz_name),
    )
    return [dict(row) for row in rows]


async def load_candidates(conn, *, tz_name: str, today: date, limit: int = 6) -> list[dict]:
    """Useful suggestions for today: overdue, soon due, then fresh records."""
    rows = await conn.fetch(
        f"""
        SELECT t.id, t.title, t.deadline, t.kind, p.code AS project
        FROM tasks t JOIN projects p ON p.id=t.project_id
        WHERE {_ACTIVE} AND NOT {today_plan_sql('$1', '$2')}
        ORDER BY
          CASE
            WHEN t.deadline IS NOT NULL AND t.deadline < (NOW() AT TIME ZONE 'UTC') THEN 0
            WHEN t.deadline IS NOT NULL
                 AND (t.deadline AT TIME ZONE 'UTC' AT TIME ZONE $2)::date <= $1::date + 3 THEN 1
            WHEN t.created_at >= NOW() - interval '7 days' THEN 2
            ELSE 3
          END,
          CASE WHEN t.deadline IS NOT NULL AND t.deadline < (NOW() AT TIME ZONE 'UTC')
               THEN t.deadline END DESC,
          t.deadline ASC NULLS LAST,
          t.created_at DESC,
          t.id DESC
        LIMIT $3
        """,
        today,
        resolve_tz_name(tz_name),
        int(limit),
    )
    return [dict(row) for row in rows]
