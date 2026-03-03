"""Webhook helpers.

Render deployments and Telegram hiccups can occasionally clear or misconfigure
the webhook. This module provides small, dependency-free helpers to set and
periodically refresh the webhook.

Logging is done via the project's structured logger (JSON by default).
"""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter

from bot.services.logger import get_logger


log = get_logger("bot.webhook")


async def delayed_set_webhook(bot: Bot, desired_url: str) -> None:
    """Set webhook after a short delay so the web server is already listening."""
    await asyncio.sleep(2)
    for attempt in range(3):
        try:
            await asyncio.wait_for(bot.set_webhook(desired_url), timeout=5)
            info = await asyncio.wait_for(bot.get_webhook_info(), timeout=5)
            log.info(
                "Webhook set",
                url=getattr(info, "url", ""),
                pending=getattr(info, "pending_update_count", None),
                last_error=getattr(info, "last_error_message", None),
            )
            return
        except TelegramRetryAfter as e:
            await asyncio.sleep(float(getattr(e, "retry_after", 1.0)) + 0.2)
        except Exception as e:
            log.warning(
                "Webhook set attempt failed",
                attempt=attempt + 1,
                desired_url=desired_url,
                error_type=type(e).__name__,
                error_message=str(e),
            )
            await asyncio.sleep(2)


def make_maybe_refresh_webhook(
    *,
    bot: Bot,
    webhook_url: str,
    webhook_path: str,
    refresh_every_sec: int,
) -> Callable[[], Awaitable[None]]:
    """Return a rate-limited coroutine that keeps webhook configured."""

    last_refresh_ts = 0.0
    desired = f"{(webhook_url or '').rstrip('/')}{webhook_path}" if webhook_url else ""

    async def _maybe_refresh() -> None:
        nonlocal last_refresh_ts
        if not webhook_url:
            return
        now_ts = time.time()
        if now_ts - last_refresh_ts < float(refresh_every_sec):
            return
        last_refresh_ts = now_ts
        try:
            info = await asyncio.wait_for(bot.get_webhook_info(), timeout=5)
            current = getattr(info, "url", "")
            if current != desired:
                log.warning("Webhook URL mismatch. Resetting", current=current, desired=desired)
                await asyncio.wait_for(bot.set_webhook(desired), timeout=5)
        except Exception as e:
            log.warning(
                "Webhook refresh failed",
                error_type=type(e).__name__,
                error_message=str(e),
            )

    return _maybe_refresh


async def webhook_keeper(
    maybe_refresh: Callable[[], Awaitable[None]],
    *,
    keeper_every_sec: int,
) -> None:
    """Background task to keep webhook configured."""
    await asyncio.sleep(5)
    while True:
        try:
            await maybe_refresh()
        except Exception as e:
            log.warning(
                "Webhook keeper error",
                error_type=type(e).__name__,
                error_message=str(e),
            )
        await asyncio.sleep(float(keeper_every_sec))
