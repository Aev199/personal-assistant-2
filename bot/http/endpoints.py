"""aiohttp endpoints.

Keep operational HTTP endpoints (cron tick, health, backup) out of the Telegram
handlers.

All endpoints use the project's structured logger (via ``deps.logger``) when
available, falling back to a module logger.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

import asyncpg
from aiohttp import web
from aiogram import Bot

from bot.adapters.storage_adapter import DropboxStorageAdapter, GCSStorageAdapter, S3StorageAdapter
from bot.deps import AppDeps
from bot.services.backup import create_backup_service
from bot.services.background import fire_and_forget
from bot.services.logger import StructuredLogger, get_logger
from bot.services.tick import do_tick as do_tick_service


AsyncCallable = Callable[[], Awaitable[None]]

# Cross-process locks (Postgres advisory locks)
LOCK_KEY_TICK = 912_345_001
LOCK_KEY_BACKUP = 912_345_002


def _log_from(ctx_deps: AppDeps | None) -> StructuredLogger:
    if ctx_deps is not None and getattr(ctx_deps, "logger", None) is not None:
        return ctx_deps.logger  # type: ignore[return-value]
    return get_logger("bot.http")


@dataclass(slots=True)
class HttpContext:
    bot: Bot
    deps: AppDeps

    # env/config
    database_url: str
    internal_api_key: str
    allow_public_tick: bool
    tick_timeout_sec: float
    tick_send_timeout_sec: float
    icloud_enabled: bool
    mode: str = "polling-web"

    # backup env/config
    backup_storage_backend: str
    backup_retention_days: int
    aws_s3_bucket: str
    aws_s3_region: str
    dropbox_access_token: str
    dropbox_backup_path: str
    gcs_bucket: str
    gcs_project_id: str
    gcs_credentials_json: str

    # webhook self-heal (optional)
    refresh_webhook: Optional[Callable[[], Awaitable[None]]] = None
    started_at_ts: float = 0.0
    tick_lock: asyncio.Lock | None = None
    backup_lock: asyncio.Lock | None = None
    last_tick_started_at: float | None = None
    last_tick_finished_at: float | None = None
    last_tick_status: str = "never"
    last_tick_result: dict[str, Any] | None = None


def attach_routes(app: web.Application, ctx: HttpContext) -> None:
    """Register HTTP routes on aiohttp app."""

    async def _tick(request: web.Request) -> web.StreamResponse:
        return await handle_cron_tick(request, ctx)

    async def _ping(request: web.Request) -> web.StreamResponse:
        return await handle_ping(request)

    async def _health(request: web.Request) -> web.StreamResponse:
        return await handle_health(request, ctx)

    async def _internal_status(request: web.Request) -> web.StreamResponse:
        return await handle_internal_status(request, ctx)

    async def _backup(request: web.Request) -> web.StreamResponse:
        return await handle_backup(request, ctx)

    async def _keepalive(request: web.Request) -> web.StreamResponse:
        return await handle_keepalive(request, ctx)

    app.router.add_get("/tick", _tick)
    app.router.add_get("/ping", _ping)
    app.router.add_get("/health", _health)
    app.router.add_get("/internal/status", _internal_status)
    app.router.add_get("/keepalive", _keepalive)
    app.router.add_post("/backup", _backup)


def _authorized(request: web.Request, ctx: HttpContext) -> bool:
    if ctx.allow_public_tick:
        return True
    if not ctx.internal_api_key:
        return False
    return request.headers.get("X-Internal-Key", "") == ctx.internal_api_key


async def handle_cron_tick(request: web.Request, ctx: HttpContext) -> web.StreamResponse:
    log = _log_from(ctx.deps)

    # Protection (required by default)
    if not _authorized(request, ctx):
        if not ctx.internal_api_key:
            return web.Response(status=403, text="INTERNAL_API_KEY not configured")
        return web.Response(status=403, text="Forbidden")

    lock = ctx.tick_lock or asyncio.Lock()
    if lock.locked():
        return web.Response(text="BUSY")

    pool: asyncpg.Pool | None = ctx.deps.db_pool
    if not pool:
        return web.Response(status=500, text="No DB pool")

    # Cross-process lock (prevents concurrent tick across workers/instances)
    try:
        async with pool.acquire() as lock_conn:
            got = await lock_conn.fetchval("SELECT pg_try_advisory_lock($1)", LOCK_KEY_TICK)
            if not got:
                return web.Response(text="BUSY")

            try:
                ctx.last_tick_started_at = time.time()
                ctx.last_tick_status = "running"
                async with lock:
                    result = await asyncio.wait_for(
                        do_tick_service(
                            pool,
                            bot=ctx.bot,
                            admin_id=ctx.deps.admin_id,
                            tz_name=ctx.deps.tz_name,
                            send_timeout_sec=ctx.tick_send_timeout_sec,
                            icloud=ctx.deps.icloud,
                            icloud_enabled=ctx.icloud_enabled,
                            error_logger=ctx.deps.db_log_error,  # optional
                            logger=getattr(ctx.deps, "logger", None),
                        ),
                        timeout=ctx.tick_timeout_sec,
                    )
                ctx.last_tick_result = result
                ctx.last_tick_status = "ok"
            except asyncio.TimeoutError:
                log.warning("tick timeout", timeout_sec=ctx.tick_timeout_sec)
                ctx.last_tick_status = "timeout"
                ctx.last_tick_result = {"ok": False, "error": "timeout"}
                return web.json_response({"ok": False, "error": "timeout"}, status=504)
            except Exception as e:
                log.error("tick failed", error=e)
                err_logger = ctx.deps.db_log_error
                if err_logger:
                    fire_and_forget(err_logger("tick", e), label="err:tick")
                ctx.last_tick_status = "failed"
                ctx.last_tick_result = {"ok": False, "error": str(e)}
                return web.json_response({"ok": False, "error": str(e)}, status=500)
            finally:
                ctx.last_tick_finished_at = time.time()
                try:
                    await lock_conn.execute("SELECT pg_advisory_unlock($1)", LOCK_KEY_TICK)
                except Exception:
                    pass
    except Exception as e:
        log.error("tick lock failed", error=e)
        return web.Response(status=500, text="Lock error")

    # keep webhook alive (self-heal)
    if ctx.refresh_webhook is not None:
        try:
            await ctx.refresh_webhook()
        except Exception as e:
            log.warning("refresh_webhook failed", error_type=type(e).__name__, error_message=str(e))

    return web.json_response(ctx.last_tick_result or {"ok": True})


async def handle_ping(request: web.Request) -> web.StreamResponse:
    return web.Response(text="OK")


async def handle_health(request: web.Request, ctx: HttpContext) -> web.StreamResponse:
    """Readiness check.

    - `/ping` is liveness (process is up).
    - `/health` is readiness (DB available). External integrations are reported
      as *degraded* and should not restart the service.
    """

    status: dict = {
        "ok": True,
        "ready": True,
        "degraded": [],
        "uptime_sec": int(time.time() - (ctx.started_at_ts or time.time())),
        "db": False,
        "webdav": None,
        "mode": ctx.mode,
    }

    # DB check (readiness gate)
    pool: asyncpg.Pool | None = ctx.deps.db_pool
    if pool:
        try:
            async with pool.acquire() as conn:
                await asyncio.wait_for(conn.fetchval("SELECT 1"), timeout=2)
            status["db"] = True
        except Exception as e:
            status["ok"] = False
            status["ready"] = False
            status["db_error"] = str(e)
    else:
        status["ok"] = False
        status["ready"] = False
        status["db_error"] = "no_pool"

    # WebDAV check (degraded only)
    try:
        ping = getattr(ctx.deps.cloud, "ping", None)
        if callable(ping):
            status["webdav"] = await asyncio.wait_for(ping(), timeout=3)
        else:
            status["webdav"] = None
        if status["webdav"] is False:
            status["degraded"].append("webdav")
    except Exception as e:
        status["webdav"] = False
        status["degraded"].append("webdav")
        status["webdav_error"] = str(e)

    http_status = 200 if status["ready"] else 503
    return web.json_response(status, status=http_status)


async def handle_internal_status(request: web.Request, ctx: HttpContext) -> web.StreamResponse:
    if not _authorized(request, ctx):
        return web.Response(status=403, text="Forbidden")
    pool: asyncpg.Pool | None = ctx.deps.db_pool
    due_backlog_count = 0
    retry_backlog_count = 0
    oldest_due_age_sec = 0
    if pool:
        async with pool.acquire() as conn:
            due_backlog_count = int(
                await conn.fetchval(
                    """
                    SELECT COUNT(*) FROM reminders
                    WHERE status = 'pending' AND next_attempt_at_utc <= NOW()
                    """
                )
                or 0
            )
            retry_backlog_count = int(
                await conn.fetchval(
                    """
                    SELECT COUNT(*) FROM reminders
                    WHERE status = 'retry'
                    """
                )
                or 0
            )
            oldest_due_age_sec = int(
                await conn.fetchval(
                    """
                    SELECT COALESCE(EXTRACT(EPOCH FROM (NOW() - MIN(next_attempt_at_utc))), 0)
                    FROM reminders
                    WHERE status IN ('pending', 'retry') AND next_attempt_at_utc <= NOW()
                    """
                )
                or 0
            )
    body = {
        "ok": True,
        "mode": ctx.mode,
        "last_tick_started_at": ctx.last_tick_started_at,
        "last_tick_finished_at": ctx.last_tick_finished_at,
        "last_tick_status": ctx.last_tick_status,
        "last_tick_result": ctx.last_tick_result or {},
        "due_backlog_count": due_backlog_count,
        "retry_backlog_count": retry_backlog_count,
        "oldest_due_age_sec": oldest_due_age_sec,
        "polling_alive": bool(request.app.get("polling_task") and not request.app["polling_task"].done()),
    }
    return web.json_response(body)


async def handle_backup(request: web.Request, ctx: HttpContext) -> web.StreamResponse:
    """Backup endpoint for Render cron job."""

    log = _log_from(ctx.deps)

    # Protection (required by default)
    if not _authorized(request, ctx):
        if not ctx.internal_api_key:
            return web.Response(status=403, text="INTERNAL_API_KEY not configured")
        return web.Response(status=403, text="Forbidden")

    if not ctx.backup_storage_backend:
        log.warning("Backup endpoint called but BACKUP_STORAGE_BACKEND not configured")
        return web.Response(status=503, text="Backup not configured")

    if not ctx.database_url:
        log.error("DATABASE_URL not configured")
        return web.Response(status=500, text="DATABASE_URL not configured")

    pool: asyncpg.Pool | None = ctx.deps.db_pool
    if not pool:
        return web.Response(status=500, text="No DB pool")

    lock = ctx.backup_lock or asyncio.Lock()
    if lock.locked():
        return web.Response(text="BUSY")

    # Cross-process lock (prevents concurrent backups across workers/instances)
    try:
        async with pool.acquire() as lock_conn:
            got = await lock_conn.fetchval("SELECT pg_try_advisory_lock($1)", LOCK_KEY_BACKUP)
            if not got:
                return web.Response(text="BUSY")

            try:
                async with lock:
                    # Storage backend
                    if ctx.backup_storage_backend == "s3":
                        if not ctx.aws_s3_bucket:
                            return web.Response(status=503, text="AWS_S3_BUCKET not configured")
                        storage_adapter = S3StorageAdapter(bucket=ctx.aws_s3_bucket, region=ctx.aws_s3_region)
                    elif ctx.backup_storage_backend == "dropbox":
                        if not ctx.dropbox_access_token:
                            return web.Response(status=503, text="DROPBOX_ACCESS_TOKEN not configured")
                        storage_adapter = DropboxStorageAdapter(
                            access_token=ctx.dropbox_access_token,
                            base_path=ctx.dropbox_backup_path,
                        )
                    elif ctx.backup_storage_backend == "gcs":
                        if not ctx.gcs_bucket:
                            return web.Response(status=503, text="GCS_BUCKET not configured")
                        storage_adapter = GCSStorageAdapter(
                            bucket=ctx.gcs_bucket,
                            project_id=ctx.gcs_project_id,
                            credentials_json=ctx.gcs_credentials_json,
                        )
                    else:
                        return web.Response(
                            status=503,
                            text=f"Invalid BACKUP_STORAGE_BACKEND: {ctx.backup_storage_backend}",
                        )

                    backup_logger = getattr(ctx.deps, "logger", None) or get_logger("bot.backup")
                    backup_service = create_backup_service(
                        db_url=ctx.database_url,
                        storage_adapter=storage_adapter,
                        logger=backup_logger,
                    )

                    log.info("Starting scheduled backup", backend=ctx.backup_storage_backend)
                    result = await backup_service.create_backup()

                    if result.success:
                        log.info(
                            "Backup completed successfully",
                            backup_id=result.backup_id,
                            file_path=result.file_path,
                            file_size_bytes=result.file_size_bytes,
                            duration_seconds=result.duration_seconds,
                        )
                        return web.json_response(
                            {
                                "ok": True,
                                "backup_id": result.backup_id,
                                "file_path": result.file_path,
                                "file_size_bytes": result.file_size_bytes,
                                "duration_seconds": result.duration_seconds,
                            }
                        )

                    log.error(
                        "Backup failed",
                        error_message=result.error_message,
                        backup_id=result.backup_id,
                        duration_seconds=result.duration_seconds,
                    )
                    return web.json_response(
                        {
                            "ok": False,
                            "backup_id": result.backup_id,
                            "error": result.error_message,
                            "duration_seconds": result.duration_seconds,
                        },
                        status=500,
                    )
            finally:
                try:
                    await lock_conn.execute("SELECT pg_advisory_unlock($1)", LOCK_KEY_BACKUP)
                except Exception:
                    pass
    except Exception as e:
        log.error("backup lock failed", error=e)
        return web.Response(status=500, text="Lock error")


async def handle_keepalive(request: web.Request, ctx: HttpContext) -> web.StreamResponse:
    return web.json_response(
        {
            "ok": True,
            "mode": ctx.mode,
            "uptime_sec": int(time.time() - (ctx.started_at_ts or time.time())),
        }
    )
