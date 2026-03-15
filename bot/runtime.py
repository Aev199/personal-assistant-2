"""App runtime / entrypoint."""

from __future__ import annotations

import asyncio
import os
import time

from aiohttp import web
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from dotenv import load_dotenv

from bot.bootstrap import build_core
from bot.config import load_config
from bot.http.endpoints import HttpContext, attach_routes
from bot.lifecycle import make_on_shutdown, make_on_startup
from bot.services.error_handler import create_error_handler
from bot.services.logger import configure_logging, get_logger
from bot.services.webhook import make_maybe_refresh_webhook
from bot.tz import resolve_tz_name


async def _noop_async() -> None:
    return None


def _runtime_mode() -> str:
    raw = (os.getenv("BOT_RUNTIME_MODE") or "").strip().lower()
    if raw in {"webhook", "polling-web", "auto"}:
        return raw
    if raw:
        raise RuntimeError("BOT_RUNTIME_MODE must be one of: webhook, polling-web, auto")
    if os.getenv("RENDER_EXTERNAL_URL") or os.getenv("RENDER_SERVICE_ID") or os.getenv("RENDER"):
        return "webhook"
    return "auto"


def _admin_id(cfg_admin_id: int | None = None) -> int:
    """Resolve admin id from env with cfg fallback."""
    raw = os.getenv("ADMIN_ID")
    if raw is None or raw == "":
        return int(cfg_admin_id or 0)
    try:
        return int(raw or 0)
    except Exception:
        return int(cfg_admin_id or 0)


def create_app_webhook() -> web.Application:
    """Create aiohttp application for webhook mode."""

    load_dotenv()

    # Configure logging early (env defaults) so misconfig errors are visible.
    configure_logging(level=os.getenv("LOG_LEVEL", "INFO"), fmt=os.getenv("LOG_FORMAT", "json"))

    cfg = load_config()

    # Apply config-driven logging (idempotent; mostly sets level/format expectations)
    configure_logging(level=cfg.logging.level, fmt=cfg.logging.format)
    log = get_logger("bot.runtime")

    # tz is resolved in config (prefers BOT_TIMEZONE/APP_TIMEZONE over TZ)
    tz_name = (cfg.bot.timezone or "Europe/Moscow")
    admin_id = _admin_id(cfg.bot.admin_id)

    bot, dp, cloud, vault, gtasks, icloud, llm = build_core(
        bot_token=cfg.bot.token,
        admin_id=admin_id,
        tz_name=tz_name,
        google_client_id=os.getenv("GOOGLE_CLIENT_ID", ""),
        google_client_secret=os.getenv("GOOGLE_CLIENT_SECRET", ""),
        google_refresh_token=os.getenv("GOOGLE_REFRESH_TOKEN", ""),
        icloud_apple_id=os.getenv("ICLOUD_APPLE_ID", ""),
        icloud_app_password=os.getenv("ICLOUD_APP_PASSWORD", ""),
    )

    deps = dp.workflow_data.get("deps")
    if deps is None:
        raise RuntimeError("Deps container not found in dispatcher workflow_data")

    # Normalize timezone: always prefer explicit env vars over defaults.
    deps.tz_name = resolve_tz_name(deps.tz_name or tz_name)

    # Attach runtime services to deps
    deps.logger = get_logger("bot")
    deps.error_notify_user = bool(cfg.error_handler.notify_user)
    deps.error_notify_admin = bool(cfg.error_handler.notify_admin)
    if deps.admin_id <= 0:
        deps.error_notify_admin = False
    deps.error_handler = create_error_handler(
        bot=bot,
        admin_id=deps.admin_id,
        logger=get_logger("bot.error_handler"),
        max_notifications_per_minute=int(cfg.error_handler.rate_limit or 5),
    )

    database_url = cfg.database.url

    # Webhook
    webhook_url = (cfg.bot.webhook_url or "").rstrip("/")
    webhook_path = os.getenv("WEBHOOK_PATH", "/webhook")
    if not webhook_url:
        raise RuntimeError("WEBHOOK_URL is not configured for webhook mode")

    log.info(
        "runtime configured",
        mode="webhook",
        webhook_path=webhook_path,
        webhook_url=webhook_url,
        admin_id=admin_id,
        tz_name=tz_name,
        deps_tz_name=getattr(deps, "tz_name", None),
    )

    webhook_refresh_every_sec = int(os.getenv("WEBHOOK_REFRESH_EVERY_SEC", "300"))
    webhook_keeper_every_sec = int(os.getenv("WEBHOOK_KEEPER_EVERY_SEC", "120"))

    maybe_refresh_webhook = make_maybe_refresh_webhook(
        bot=bot,
        webhook_url=webhook_url,
        webhook_path=webhook_path,
        refresh_every_sec=webhook_refresh_every_sec,
        secret_token=cfg.bot.webhook_secret_token,
    )

    tick_lock = asyncio.Lock()
    backup_lock = asyncio.Lock()

    # Lifecycle
    dp.startup.register(
        make_on_startup(
            dp=dp,
            cloud=cloud,
            gtasks=gtasks,
            icloud=icloud,
            llm=llm,
            database_url=database_url,
            webhook_url=webhook_url,
            webhook_path=webhook_path,
            webhook_secret_token=cfg.bot.webhook_secret_token,
            webhook_keeper_every_sec=webhook_keeper_every_sec,
            maybe_refresh_webhook=maybe_refresh_webhook,
        )
    )
    dp.shutdown.register(make_on_shutdown(dp=dp, cloud=cloud, gtasks=gtasks, icloud=icloud, llm=llm))

    app = web.Application()

    webhook_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        handle_in_background=True,
        secret_token=cfg.bot.webhook_secret_token or None,
    )
    webhook_handler.register(app, path=webhook_path)
    setup_application(app, dp, bot=bot)

    # Auxiliary HTTP endpoints (kept out of Telegram handlers)
    allow_public_tick = os.getenv("ALLOW_PUBLIC_TICK", "").lower() in ("1", "true", "yes")

    ctx = HttpContext(
        bot=bot,
        deps=deps,
        database_url=database_url,
        internal_api_key=cfg.bot.internal_api_key,
        allow_public_tick=allow_public_tick,
        tick_timeout_sec=float(os.getenv("TICK_TIMEOUT_SEC", "25")),
        tick_send_timeout_sec=float(os.getenv("TICK_SEND_TIMEOUT_SEC", "8")),
        icloud_enabled=bool(os.getenv("ICLOUD_APPLE_ID", "") and os.getenv("ICLOUD_APP_PASSWORD", "")),
        mode="webhook",
        backup_storage_backend=os.getenv("BACKUP_STORAGE_BACKEND", ""),
        backup_retention_days=int(os.getenv("BACKUP_RETENTION_DAYS", "30")),
        aws_s3_bucket=os.getenv("AWS_S3_BUCKET", ""),
        aws_s3_region=os.getenv("AWS_S3_REGION", "us-east-1"),
        dropbox_access_token=os.getenv("DROPBOX_ACCESS_TOKEN", ""),
        dropbox_backup_path=os.getenv("DROPBOX_BACKUP_PATH", "/backups"),
        gcs_bucket=os.getenv("GCS_BUCKET", ""),
        gcs_project_id=os.getenv("GCS_PROJECT_ID", ""),
        gcs_credentials_json=os.getenv("GCS_CREDENTIALS_JSON", ""),
        refresh_webhook=maybe_refresh_webhook,
        started_at_ts=time.time(),
        tick_lock=tick_lock,
        backup_lock=backup_lock,
    )
    attach_routes(app, ctx)
    return app




def create_app_polling_web() -> web.Application:
    """Create aiohttp application for Render Free Web Service + polling mode.

    Render Web Services require an open port. In this mode we expose the same
    auxiliary endpoints (/health, /ping, /tick, /backup) and run aiogram
    long-polling in a background task.
    """

    load_dotenv()
    configure_logging(level=os.getenv("LOG_LEVEL", "INFO"), fmt=os.getenv("LOG_FORMAT", "json"))
    cfg = load_config()
    configure_logging(level=cfg.logging.level, fmt=cfg.logging.format)
    log = get_logger("bot.runtime")

    tz_name = (cfg.bot.timezone or "Europe/Moscow")
    admin_id = _admin_id(cfg.bot.admin_id)

    bot, dp, cloud, vault, gtasks, icloud, llm = build_core(
        bot_token=cfg.bot.token,
        admin_id=admin_id,
        tz_name=tz_name,
        google_client_id=os.getenv("GOOGLE_CLIENT_ID", ""),
        google_client_secret=os.getenv("GOOGLE_CLIENT_SECRET", ""),
        google_refresh_token=os.getenv("GOOGLE_REFRESH_TOKEN", ""),
        icloud_apple_id=os.getenv("ICLOUD_APPLE_ID", ""),
        icloud_app_password=os.getenv("ICLOUD_APP_PASSWORD", ""),
    )

    deps = dp.workflow_data.get("deps")
    if deps is None:
        raise RuntimeError("Deps container not found in dispatcher workflow_data")

    deps.logger = get_logger("bot")
    deps.error_notify_user = bool(cfg.error_handler.notify_user)
    deps.error_notify_admin = bool(cfg.error_handler.notify_admin)
    if deps.admin_id <= 0:
        deps.error_notify_admin = False
    deps.error_handler = create_error_handler(
        bot=bot,
        admin_id=deps.admin_id,
        logger=get_logger("bot.error_handler"),
        max_notifications_per_minute=int(cfg.error_handler.rate_limit or 5),
    )

    database_url = cfg.database.url
    log.info(
        "runtime configured",
        mode="polling-web",
        admin_id=admin_id,
        tz_name=tz_name,
    )

    # Lifecycle (no webhook in polling mode)
    dp.startup.register(
        make_on_startup(
            dp=dp,
            cloud=cloud,
            gtasks=gtasks,
            icloud=icloud,
            llm=llm,
            database_url=database_url,
            webhook_url="",
            webhook_path="",
            webhook_secret_token="",
            webhook_keeper_every_sec=0,
            maybe_refresh_webhook=_noop_async,
        )
    )
    dp.shutdown.register(make_on_shutdown(dp=dp, cloud=cloud, gtasks=gtasks, icloud=icloud, llm=llm))

    app = web.Application()

    tick_lock = asyncio.Lock()
    backup_lock = asyncio.Lock()

    ctx = HttpContext(
        bot=bot,
        deps=deps,
        database_url=database_url,
        internal_api_key=cfg.bot.internal_api_key,
        allow_public_tick=os.getenv("ALLOW_PUBLIC_TICK", "").lower() in ("1", "true", "yes"),
        tick_timeout_sec=float(os.getenv("TICK_TIMEOUT_SEC", "25")),
        tick_send_timeout_sec=float(os.getenv("TICK_SEND_TIMEOUT_SEC", "8")),
        icloud_enabled=bool(os.getenv("ICLOUD_APPLE_ID", "") and os.getenv("ICLOUD_APP_PASSWORD", "")),
        mode="polling-web",
        backup_storage_backend=os.getenv("BACKUP_STORAGE_BACKEND", ""),
        backup_retention_days=int(os.getenv("BACKUP_RETENTION_DAYS", "30")),
        aws_s3_bucket=os.getenv("AWS_S3_BUCKET", ""),
        aws_s3_region=os.getenv("AWS_S3_REGION", "us-east-1"),
        dropbox_access_token=os.getenv("DROPBOX_ACCESS_TOKEN", ""),
        dropbox_backup_path=os.getenv("DROPBOX_BACKUP_PATH", "/backups"),
        gcs_bucket=os.getenv("GCS_BUCKET", ""),
        gcs_project_id=os.getenv("GCS_PROJECT_ID", ""),
        gcs_credentials_json=os.getenv("GCS_CREDENTIALS_JSON", ""),
        refresh_webhook=_noop_async,
        started_at_ts=time.time(),
        tick_lock=tick_lock,
        backup_lock=backup_lock,
    )
    attach_routes(app, ctx)

    async def _start(_app: web.Application) -> None:
        log.info("starting polling background task (web-service mode)")
        try:
            await bot.delete_webhook(drop_pending_updates=False)
            log.info("deleted Telegram webhook before polling start")
        except Exception as e:
            log.warning("failed to delete webhook before polling start", error_type=type(e).__name__, error_message=str(e))
        _app["polling_task"] = asyncio.create_task(dp.start_polling(bot))

    async def _stop(_app: web.Application) -> None:
        task = _app.get("polling_task")
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    app.on_startup.append(_start)
    app.on_shutdown.append(_stop)

    return app

async def run_polling() -> None:
    """Run aiogram long-polling (no web server required).

    This is the recommended mode for Render Background Worker.
    """

    load_dotenv()
    configure_logging(level=os.getenv("LOG_LEVEL", "INFO"), fmt=os.getenv("LOG_FORMAT", "json"))
    cfg = load_config()
    configure_logging(level=cfg.logging.level, fmt=cfg.logging.format)

    tz_name = (cfg.bot.timezone or "Europe/Moscow")
    admin_id = _admin_id(cfg.bot.admin_id)

    bot, dp, cloud, vault, gtasks, icloud, llm = build_core(
        bot_token=cfg.bot.token,
        admin_id=admin_id,
        tz_name=tz_name,
        google_client_id=os.getenv("GOOGLE_CLIENT_ID", ""),
        google_client_secret=os.getenv("GOOGLE_CLIENT_SECRET", ""),
        google_refresh_token=os.getenv("GOOGLE_REFRESH_TOKEN", ""),
        icloud_apple_id=os.getenv("ICLOUD_APPLE_ID", ""),
        icloud_app_password=os.getenv("ICLOUD_APP_PASSWORD", ""),
    )

    deps = dp.workflow_data.get("deps")
    if deps is None:
        raise RuntimeError("Deps container not found in dispatcher workflow_data")

    deps.logger = get_logger("bot")
    deps.error_notify_user = bool(cfg.error_handler.notify_user)
    deps.error_notify_admin = bool(cfg.error_handler.notify_admin)
    if deps.admin_id <= 0:
        deps.error_notify_admin = False
    deps.error_handler = create_error_handler(
        bot=bot,
        admin_id=deps.admin_id,
        logger=get_logger("bot.error_handler"),
        max_notifications_per_minute=int(cfg.error_handler.rate_limit or 5),
    )

    database_url = cfg.database.url
    # We intentionally skip webhook setup in polling mode.
    dp.startup.register(
        make_on_startup(
            dp=dp,
            cloud=cloud,
            gtasks=gtasks,
            icloud=icloud,
            llm=llm,
            database_url=database_url,
            webhook_url="",
            webhook_path="",
            webhook_secret_token="",
            webhook_keeper_every_sec=0,
            maybe_refresh_webhook=_noop_async,
        )
    )
    dp.shutdown.register(make_on_shutdown(dp=dp, cloud=cloud, gtasks=gtasks, icloud=icloud, llm=llm))

    try:
        await bot.delete_webhook(drop_pending_updates=False)
    except Exception:
        pass
    await dp.start_polling(bot)


def main() -> None:
    load_dotenv()
    cfg = load_config()
    mode = _runtime_mode()
    if mode == "webhook":
        if not (cfg.bot.webhook_url or "").strip():
            raise RuntimeError("Webhook mode requires WEBHOOK_URL or RENDER_EXTERNAL_URL")
        app = create_app_webhook()
    elif mode == "polling-web":
        app = create_app_polling_web()
    else:
        app = create_app_webhook() if (cfg.bot.webhook_url or "").strip() else create_app_polling_web()
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "10000"))
    web.run_app(app, host=host, port=port)
