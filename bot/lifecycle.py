"""Application lifecycle hooks.

Aiogram startup/shutdown hooks are responsible for:

- starting / closing integration sessions (WebDAV, Google Tasks, iCloud)
- creating and closing the asyncpg pool
- best-effort DB schema bootstrap
- webhook self-heal loop

Logging uses the project's structured logger when available.
"""

from __future__ import annotations

import asyncio
import os

import asyncpg
from aiogram import Bot, Dispatcher

from bot.db.errors import db_log_error
from bot.db.schema import ensure_schema
from bot.services.background import fire_and_forget
from bot.services.logger import StructuredLogger, get_logger
from bot.services.webhook import delayed_set_webhook, webhook_keeper


def _db_pool_params() -> tuple[int, int, float]:
    """Pool sizing defaults for Render + free DB tiers."""
    try:
        mn = int(os.getenv("DB_MIN_POOL", "1"))
    except Exception:
        mn = 1
    try:
        mx = int(os.getenv("DB_MAX_POOL", "5"))
    except Exception:
        mx = 5
    try:
        timeout = float(os.getenv("DB_COMMAND_TIMEOUT", "15"))
    except Exception:
        timeout = 15.0
    return mn, mx, timeout


def _get_log(dp: Dispatcher) -> StructuredLogger:
    deps = dp.workflow_data.get("deps")
    if deps is not None:
        lg = getattr(deps, "logger", None)
        if lg is not None:
            return lg
    return get_logger("bot.lifecycle")


def make_on_startup(
    *,
    dp: Dispatcher,
    cloud,
    gtasks,
    icloud,
    database_url: str,
    webhook_url: str,
    webhook_path: str,
    webhook_keeper_every_sec: int,
    maybe_refresh_webhook,
):
    async def _on_startup(bot: Bot) -> None:
        log = _get_log(dp)

        # Start integrations
        if hasattr(cloud, "startup"):
            await cloud.startup()

        if os.getenv("GOOGLE_REFRESH_TOKEN", ""):
            try:
                await gtasks.startup()
                log.info("Google Tasks enabled")
            except Exception as e:
                log.warning("Google Tasks startup failed", error_type=type(e).__name__, error_message=str(e))

        if os.getenv("ICLOUD_APPLE_ID", "") and os.getenv("ICLOUD_APP_PASSWORD", ""):
            try:
                await icloud.startup()
                log.info("iCloud CalDAV enabled")
            except Exception as e:
                log.warning("iCloud CalDAV startup failed", error_type=type(e).__name__, error_message=str(e))

        # DB pool
        mn, mx, timeout = _db_pool_params()
        pool = await asyncpg.create_pool(
            database_url,
            min_size=mn,
            max_size=mx,
            command_timeout=timeout,
            statement_cache_size=0,
        )
        dp.workflow_data.update({"db_pool": pool})

        # Update dependency container
        deps = dp.workflow_data.get("deps")
        if deps is not None:
            try:
                deps.db_pool = pool
            except Exception:
                pass

        # Expose a convenient error logger for places that only have access to dp.workflow_data.
        # Signature matches (where, exc, context) used across the codebase.
        _db_log_error = lambda where, exc, context=None: db_log_error(pool, where, exc, context)
        dp.workflow_data["db_log_error"] = _db_log_error
        if deps is not None:
            try:
                deps.db_log_error = _db_log_error
            except Exception:
                pass

        # Schema bootstrap
        schema_strict = os.getenv("SCHEMA_BOOTSTRAP_STRICT", "1").lower() in {"1", "true", "yes", "y"}
        try:
            async with pool.acquire() as conn:
                await ensure_schema(conn)
            log.info("DB schema is ready")
        except Exception as e:
            log.error(
                "Schema bootstrap failed",
                error_type=type(e).__name__,
                error_message=str(e),
                strict=schema_strict,
            )
            if schema_strict:
                # Fail fast: a healthy process with a broken schema will only cause runtime errors later.
                # Close the pool to avoid leaking connections and re-raise to stop startup.
                try:
                    await pool.close()
                except Exception:
                    pass
                raise


        # Webhook: set and keep-alive
        if webhook_url:
            desired = f"{webhook_url}{webhook_path}"
            fire_and_forget(delayed_set_webhook(bot, desired), label="webhook:set")
            try:
                dp.workflow_data["webhook_keeper_task"] = asyncio.create_task(
                    webhook_keeper(maybe_refresh_webhook, keeper_every_sec=webhook_keeper_every_sec)
                )
            except Exception:
                pass

    return _on_startup


def make_on_shutdown(*, dp: Dispatcher, cloud, gtasks, icloud):
    async def _on_shutdown(bot: Bot) -> None:
        log = _get_log(dp)

        if hasattr(cloud, "close"):
            try:
                await cloud.close()
            except Exception:
                pass

        if os.getenv("GOOGLE_REFRESH_TOKEN", ""):
            try:
                await gtasks.close()
            except Exception:
                pass

        if os.getenv("ICLOUD_APPLE_ID", "") and os.getenv("ICLOUD_APP_PASSWORD", ""):
            try:
                await icloud.close()
            except Exception:
                pass

        if os.getenv("DELETE_WEBHOOK_ON_SHUTDOWN", "0") == "1":
            try:
                await bot.delete_webhook()
            except Exception:
                pass
        else:
            log.info("Keeping webhook on shutdown", delete_webhook_on_shutdown=False)

        pool = dp.workflow_data.get("db_pool")
        if pool:
            try:
                await pool.close()
            except Exception:
                pass

        deps = dp.workflow_data.get("deps")
        if deps is not None:
            try:
                deps.db_pool = None
                deps.db_log_error = None
            except Exception:
                pass

        t = dp.workflow_data.get("webhook_keeper_task")
        if t:
            try:
                t.cancel()
            except Exception:
                pass

        # Close underlying aiohttp session used by aiogram Bot
        try:
            await bot.session.close()
        except Exception:
            pass

    return _on_shutdown
