"""Vault file watcher: detect task status changes from Obsidian and sync to DB.

Runs as a standalone background process alongside the bot.
Polls .md files in the vault directory for changes to `- [x]` / `- [ ]` lines
and updates the database accordingly.

Usage:
    python -m bot.services.vault_watcher

Requires env:
    DATABASE_URL, ADMIN_ID, BOT_TOKEN, VAULT_LOCAL_PATH
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from datetime import datetime, timezone

import asyncpg

# Task line pattern: `- [x] assignee: title 📅 ... ⏫ ➕ ... (ID: 42)`
_TASK_LINE_RE = re.compile(
    r"-\s+\[(?P<status_char>[ x])\]\s+(?P<assignee>[^:]+)?:?\s*(?P<title>.*?)\s*\(ID:\s*(?P<task_id>\d+)\)"
)

# Markers used by VaultManager
_TASKS_BEGIN = "<!-- BOT:BEGIN TASKS -->"
_TASKS_END = "<!-- BOT:END TASKS -->"

logger = logging.getLogger("vault_watcher")


def _parse_tasks_from_md(content: str) -> dict[int, str]:
    """Extract task IDs and their checkbox status from markdown content.

    Returns dict of {task_id: status_char} where status_char is ' ' or 'x'.
    Only looks inside BOT:BEGIN/BOT:END markers.
    """
    if _TASKS_BEGIN not in content or _TASKS_END not in content:
        return {}

    _, rest = content.split(_TASKS_BEGIN, 1)
    tasks_block, _ = rest.split(_TASKS_END, 1)

    result: dict[int, str] = {}
    for line in tasks_block.split("\n"):
        match = _TASK_LINE_RE.search(line)
        if not match:
            continue
        try:
            task_id = int(match.group("task_id"))
            status_char = match.group("status_char")  # ' ' or 'x'
            result[task_id] = status_char
        except (ValueError, IndexError):
            continue
    return result


async def _check_and_sync(
    conn: asyncpg.Connection,
    local_statuses: dict[int, str],  # {task_id: 'x' or ' '}
) -> tuple[int, int, list[str]]:
    """Compare local file status with DB, sync differences.

    Returns (done_count, reopened_count, toast_lines).
    Toast lines are stored in user_settings.ui_payload for SPA display.
    """
    if not local_statuses:
        return 0, 0, []

    task_ids = list(local_statuses.keys())
    rows = await conn.fetch(
        """
        SELECT t.id, t.title, t.status, p.code AS project_code
        FROM tasks t
        JOIN projects p ON t.project_id = p.id
        WHERE t.id = ANY($1::bigint[])
        """,
        task_ids,
    )

    done_count = 0
    reopened_count = 0
    toasts: list[str] = []

    for row in rows:
        task_id = int(row["id"])
        db_status = str(row["status"] or "todo").lower()
        file_status_char = local_statuses.get(task_id, " ")
        file_done = file_status_char == "x"

        if file_done and db_status != "done":
            await conn.execute(
                "UPDATE tasks SET status='done', updated_at=NOW() WHERE id=$1",
                task_id,
            )
            done_count += 1
            title = str(row["title"] or "?")
            project = str(row["project_code"] or "")
            label = f"✅ Obsidian: {title}" + (f" [{project}]" if project else "")
            toasts.append(label)

        elif not file_done and db_status == "done":
            await conn.execute(
                "UPDATE tasks SET status='todo', updated_at=NOW() WHERE id=$1",
                task_id,
            )
            reopened_count += 1

    return done_count, reopened_count, toasts


async def _scan_vault(
    db_pool: asyncpg.Pool,
    vault_path: str,
    *,
    file_mtimes: dict[str, float],
) -> dict[str, float]:
    """Scan all .md files in vault_path, compare with cached mtimes,
    and sync any changed files.

    Returns updated file_mtimes dict.
    """
    base = os.path.expanduser(vault_path)
    if not os.path.isdir(base):
        return file_mtimes

    total_done = 0
    total_reopened = 0
    all_toasts: list[str] = []
    new_mtimes: dict[str, float] = {}

    for root, _dirs, files in os.walk(base):
        for fname in files:
            if not fname.endswith(".md"):
                continue
            full_path = os.path.join(root, fname)
            try:
                mtime = os.path.getmtime(full_path)
            except OSError:
                continue

            rel_path = os.path.relpath(full_path, base)
            new_mtimes[rel_path] = mtime

            if rel_path in file_mtimes and file_mtimes[rel_path] >= mtime:
                continue  # not modified

            # File is new or modified — parse and sync
            try:
                with open(full_path, "r", encoding="utf-8") as f:
                    content = f.read()
            except Exception:
                continue

            local_statuses = _parse_tasks_from_md(content)
            if not local_statuses:
                continue

            try:
                async with db_pool.acquire() as conn:
                    done, reopened, toasts = await _check_and_sync(
                        conn, local_statuses,
                    )
                    total_done += done
                    total_reopened += reopened
                    all_toasts.extend(toasts)
            except Exception:
                logger.exception("sync failed for %s", rel_path)

    # Store toasts in user_settings for SPA display
    if all_toasts:
        try:
            async with db_pool.acquire() as conn:
                # Get admin chat_id from user_settings
                row = await conn.fetchrow(
                    "SELECT chat_id FROM user_settings LIMIT 1"
                )
                if row:
                    chat_id = int(row["chat_id"])
                    import json as _json
                    toast_payload = _json.dumps(
                        {"toast": "\n".join(all_toasts), "toast_ttl": 25},
                        ensure_ascii=False,
                    )
                    # Merge with existing payload
                    existing = await conn.fetchval(
                        "SELECT ui_payload FROM user_settings WHERE chat_id=$1",
                        chat_id,
                    )
                    if isinstance(existing, str):
                        try:
                            existing = _json.loads(existing)
                        except Exception:
                            existing = {}
                    elif not isinstance(existing, dict):
                        existing = {}
                    existing["toast"] = "\n".join(all_toasts)
                    existing["toast_ttl"] = 25
                    await conn.execute(
                        "UPDATE user_settings SET ui_payload=$2::jsonb, updated_at=NOW() WHERE chat_id=$1",
                        chat_id,
                        _json.dumps(existing, ensure_ascii=False),
                    )
        except Exception:
            logger.exception("failed to store toast")

    if total_done or total_reopened:
        logger.info(
            "vault scan: done=%d reopened=%d files=%d",
            total_done, total_reopened, len(new_mtimes),
        )

    return new_mtimes


async def run_vault_watcher() -> None:
    """Main loop: poll vault directory and sync changes to DB."""
    logging.basicConfig(
        level=getattr(logging, os.getenv("LOG_LEVEL", "INFO")),
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )

    database_url = os.getenv("DATABASE_URL", "")
    vault_path = os.getenv("VAULT_LOCAL_PATH", "/srv/vault")
    poll_interval = float(os.getenv("VAULT_WATCH_INTERVAL_SEC", "10"))

    if not database_url:
        logger.error("Missing env: DATABASE_URL")
        return

    if not os.path.isdir(vault_path):
        logger.error("Vault directory not found: %s", vault_path)
        return

    pool = await asyncpg.create_pool(database_url, min_size=1, max_size=2)

    file_mtimes: dict[str, float] = {}
    logger.info("vault watcher started, path=%s interval=%.0fs", vault_path, poll_interval)

    try:
        while True:
            try:
                file_mtimes = await _scan_vault(
                    pool, vault_path, file_mtimes=file_mtimes,
                )
            except Exception:
                logger.exception("scan cycle failed")
            await asyncio.sleep(poll_interval)
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(run_vault_watcher())
