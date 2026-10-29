"""Process entry point: one aiohttp server for Telegram, the API and the web app.

Telegram updates arrive by webhook when WEBHOOK_URL is set, otherwise by
long polling inside the same process. Either way the HTTP API (iPhone, widget,
web interface) and the /tick endpoint for the minute cron stay on one port.
"""

from __future__ import annotations

import asyncio
import os
import time

from aiohttp import web
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from dotenv import load_dotenv

from bot.bootstrap import build_core
from bot.config import load_config
from bot.http.companion import attach_companion_routes
from bot.http.endpoints import HttpContext, attach_routes
from bot.http.web_app import attach_web_routes
from bot.lifecycle import make_on_shutdown, make_on_startup
from bot.services.error_handler import create_error_handler
from bot.services.logger import configure_logging, get_logger
from bot.services.webhook import make_maybe_refresh_webhook
from bot.tz import resolve_tz_name


async def _noop_async() -> None:
    return None


def _use_webhook(webhook_url: str) -> bool:
    mode = (os.getenv("BOT_RUNTIME_MODE") or "auto").strip().lower()
    if mode not in {"webhook", "polling-web", "auto"}:
        raise RuntimeError("BOT_RUNTIME_MODE must be one of: webhook, polling-web, auto")
    if mode == "webhook" and not webhook_url:
        raise RuntimeError("Webhook mode requires WEBHOOK_URL")
    return mode == "webhook" or (mode == "auto" and bool(webhook_url))


def _admin_id(cfg_admin_id: int | None = None) -> int:
    raw = os.getenv("ADMIN_ID")
    try:
        return int(raw) if raw else int(cfg_admin_id or 0)
    except ValueError:
        return int(cfg_admin_id or 0)


def create_app() -> web.Application:
    load_dotenv()
    configure_logging(level=os.getenv("LOG_LEVEL", "INFO"), fmt=os.getenv("LOG_FORMAT", "json"))
    cfg = load_config()
    configure_logging(level=cfg.logging.level, fmt=cfg.logging.format)
    log = get_logger("bot.runtime")

    tz_name = cfg.bot.timezone or "Europe/Moscow"
    admin_id = _admin_id(cfg.bot.admin_id)
    icloud_enabled = bool(os.getenv("ICLOUD_APPLE_ID", "") and os.getenv("ICLOUD_APP_PASSWORD", ""))

    bot, dp, icloud, llm = build_core(
        bot_token=cfg.bot.token,
        admin_id=admin_id,
        tz_name=tz_name,
        icloud_apple_id=os.getenv("ICLOUD_APPLE_ID", ""),
        icloud_app_password=os.getenv("ICLOUD_APP_PASSWORD", ""),
    )
    deps = dp.workflow_data["deps"]
    deps.tz_name = resolve_tz_name(deps.tz_name or tz_name)
    deps.logger = get_logger("bot")
    deps.error_notify_user = bool(cfg.error_handler.notify_user)
    deps.error_notify_admin = bool(cfg.error_handler.notify_admin) and deps.admin_id > 0
    deps.error_handler = create_error_handler(
        bot=bot,
        admin_id=deps.admin_id,
        logger=get_logger("bot.error_handler"),
        max_notifications_per_minute=int(cfg.error_handler.rate_limit or 5),
    )

    webhook_url = (cfg.bot.webhook_url or "").rstrip("/")
    webhook_path = os.getenv("WEBHOOK_PATH", "/webhook")
    use_webhook = _use_webhook(webhook_url)
    maybe_refresh_webhook = (
        make_maybe_refresh_webhook(
            bot=bot,
            webhook_url=webhook_url,
            webhook_path=webhook_path,
            refresh_every_sec=int(os.getenv("WEBHOOK_REFRESH_EVERY_SEC", "300")),
            secret_token=cfg.bot.webhook_secret_token,
        )
        if use_webhook
        else _noop_async
    )
    dp.startup.register(
        make_on_startup(
            dp=dp,
            icloud=icloud,
            llm=llm,
            database_url=cfg.database.url,
            webhook_url=webhook_url if use_webhook else "",
            webhook_path=webhook_path,
            webhook_secret_token=cfg.bot.webhook_secret_token if use_webhook else "",
            webhook_keeper_every_sec=int(os.getenv("WEBHOOK_KEEPER_EVERY_SEC", "120")) if use_webhook else 0,
            maybe_refresh_webhook=maybe_refresh_webhook,
        )
    )
    dp.shutdown.register(make_on_shutdown(dp=dp, icloud=icloud, llm=llm))

    app = web.Application()
    if use_webhook:
        SimpleRequestHandler(
            dispatcher=dp,
            bot=bot,
            handle_in_background=True,
            secret_token=cfg.bot.webhook_secret_token or None,
        ).register(app, path=webhook_path)
        setup_application(app, dp, bot=bot)
    else:
        async def _start_polling(_app: web.Application) -> None:
            try:
                await bot.delete_webhook(drop_pending_updates=False)
            except Exception as exc:
                log.warning("failed to delete webhook before polling", error_type=type(exc).__name__)
            _app["polling_task"] = asyncio.create_task(dp.start_polling(bot))

        async def _stop_polling(_app: web.Application) -> None:
            task = _app.get("polling_task")
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        app.on_startup.append(_start_polling)
        app.on_shutdown.append(_stop_polling)

    log.info("runtime configured", mode="webhook" if use_webhook else "polling-web", admin_id=admin_id, tz_name=deps.tz_name)

    ctx = HttpContext(
        bot=bot,
        deps=deps,
        database_url=cfg.database.url,
        internal_api_key=cfg.bot.internal_api_key,
        allow_public_tick=os.getenv("ALLOW_PUBLIC_TICK", "").lower() in ("1", "true", "yes"),
        tick_timeout_sec=float(os.getenv("TICK_TIMEOUT_SEC", "25")),
        tick_send_timeout_sec=float(os.getenv("TICK_SEND_TIMEOUT_SEC", "8")),
        icloud_enabled=icloud_enabled,
        mode="webhook" if use_webhook else "polling-web",
        refresh_webhook=maybe_refresh_webhook,
        started_at_ts=time.time(),
        tick_lock=asyncio.Lock(),
    )
    attach_routes(app, ctx)
    attach_web_routes(app, ctx)
    attach_companion_routes(app, ctx)
    return app


def main() -> None:
    web.run_app(create_app(), host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "10000")))
