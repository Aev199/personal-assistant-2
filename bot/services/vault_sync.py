"""Vault / Obsidian sync jobs."""

from __future__ import annotations

import time

import asyncpg

from bot.services.logger import get_logger


log = get_logger("bot.services.vault_sync")


async def background_project_sync(
    project_id: int,
    db_pool: asyncpg.Pool,
    vault,
    *,
    error_logger=None,
) -> None:
    """Sync a single project file into the Obsidian Vault (WebDAV).

    Updates sync_status so the UI can display health.
    """

    start = time.monotonic()
    try:
        async with db_pool.acquire() as conn:
            # mark attempt
            try:
                await conn.execute(
                    "INSERT INTO sync_status(name, last_attempt_at) VALUES($1, NOW()) "
                    "ON CONFLICT(name) DO UPDATE SET last_attempt_at=NOW()",
                    "vault",
                )
            except Exception:
                pass

            p = await conn.fetchrow(
                "SELECT code, name, COALESCE(status,'active') AS status FROM projects WHERE id = $1",
                project_id,
            )
            if not p:
                return
            tasks = await conn.fetch(
                "SELECT t.*, COALESCE(tm.name,'—') as assignee "
                "FROM tasks t "
                "LEFT JOIN team tm ON t.assignee_id = tm.id "
                "WHERE t.project_id = $1 AND t.status != 'done'",
                project_id,
            )
            events = await conn.fetch(
                "SELECT created_at, text FROM events WHERE project_id = $1 ORDER BY created_at DESC LIMIT 30",
                project_id,
            )
        try:
            log.info("vault tz debug", tz=str(getattr(vault, "tz", None)))
        except Exception:
            pass

        await vault.sync_project_file(p["name"], tasks, events, project_status=p["status"])

        dur_ms = int((time.monotonic() - start) * 1000)
        try:
            async with db_pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO sync_status(name, last_ok_at, last_error_at, last_error, last_duration_ms) "
                    "VALUES($1, NOW(), NULL, NULL, $2) "
                    "ON CONFLICT(name) DO UPDATE SET last_ok_at=NOW(), last_error_at=NULL, last_error=NULL, last_duration_ms=$2",
                    "vault",
                    dur_ms,
                )
        except Exception:
            pass
    except Exception as e:
        log.warning(
            "vault sync failed",
            error_type=type(e).__name__,
            error_message=str(e),
            project_id=project_id,
        )
        if error_logger is not None:
            try:
                await error_logger("vault_sync", e, {"project_id": project_id})
            except Exception:
                pass
        try:
            async with db_pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO sync_status(name, last_error_at, last_error) VALUES($1, NOW(), $2) "
                    "ON CONFLICT(name) DO UPDATE SET last_error_at=NOW(), last_error=$2",
                    "vault",
                    str(e),
                )
        except Exception:
            pass


async def background_log_event(
    event_text: str,
    vault,
    *,
    error_logger=None,
) -> None:
    """Append an entry into the daily vault log."""

    try:
        await vault.log_event(str(event_text))
    except Exception as e:
        log.warning(
            "vault daily log failed",
            error_type=type(e).__name__,
            error_message=str(e),
        )
        if error_logger is not None:
            try:
                await error_logger("vault_log", e, {"text": str(event_text)[:2000]})
            except Exception:
                pass
