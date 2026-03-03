"""Background task utilities.

We intentionally track spawned tasks to avoid memory leaks when integrations are slow.

This module uses the project's structured logger so background failures appear in
logs consistently (JSON by default).
"""

from __future__ import annotations

import asyncio
import os
from typing import Awaitable, Any

from bot.services.logger import get_logger


log = get_logger("bot.background")

_bg_tasks: set[asyncio.Task] = set()
_MAX_BG_TASKS = int(os.getenv("MAX_BG_TASKS", "200"))


def fire_and_forget(coro: Awaitable[Any], *, label: str = "bg") -> None:
    """Run background coroutine without blocking the bot, with safety limits/logging."""

    if len(_bg_tasks) >= _MAX_BG_TASKS:
        log.warning(
            "Too many background tasks. Dropping new task",
            task_count=len(_bg_tasks),
            max_tasks=_MAX_BG_TASKS,
            label=label,
        )
        return

    task = asyncio.create_task(coro)

    def _done(t: asyncio.Task) -> None:
        _bg_tasks.discard(t)
        try:
            exc = t.exception()
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.error("Background task callback failed", error=e, label=label)
            return
        if exc:
            # `exc` is already an Exception instance
            try:
                log.error("Background task failed", error=exc, label=label)
            except Exception:
                # Last-resort: avoid raising from callback
                log.error("Background task failed", label=label, error_message=str(exc))

    _bg_tasks.add(task)
    task.add_done_callback(_done)
