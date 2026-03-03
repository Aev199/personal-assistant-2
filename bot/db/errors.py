"""DB helpers: error logging."""

from __future__ import annotations

import json
import traceback

import asyncpg

from bot.services.logger import get_logger


log = get_logger("bot.db")


async def db_log_error(pool: asyncpg.Pool, where: str, exc: BaseException, context: dict | None = None) -> None:
    """Best-effort error logging into DB (must never raise)."""
    try:
        err = f"{type(exc).__name__}: {exc}"
        tb = traceback.format_exc()
        ctx = json.dumps(context, ensure_ascii=False) if context else None
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO errors(where_at, error, traceback, context) VALUES($1,$2,$3,$4)",
                where,
                err,
                tb,
                ctx,
            )
    except Exception:
        # Never raise from error logging; just log the failure.
        log.exception("db_log_error failed", where=where)
