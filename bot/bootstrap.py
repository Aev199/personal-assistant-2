"""Bootstrap helpers.

This module centralizes creation of the aiogram Bot/Dispatcher and wiring of
external integrations (WebDAV/Obsidian vault, Google Tasks, iCloud CalDAV).
The refactor keeps production runtime in :mod:`bot.runtime` + modular handlers.
"""

from __future__ import annotations

import os

from aiogram import Bot, Dispatcher

from bot.adapters.deepseek_adapter import DeepSeekAdapter
from bot.adapters.gemini_adapter import GeminiAdapter
from bot.adapters.google_tasks_adapter import GoogleTasksAdapter
from bot.adapters.icloud_caldav_adapter import ICloudCalDAVAdapter, ICloudCalDAVAuth
from bot.adapters.llm_router import ResilientLLMAdapter
from bot.adapters.webdav_adapter import WebDavAdapter
from bot.config import load_config
from bot.deps import AppDeps
from bot.middlewares.fsm_persistence import FsmPersistenceMiddleware
from bot.middlewares.guards import ProcessedUpdateMiddleware, SingleUserGuardMiddleware
from bot.services.onboarding_import import install_onboarding_import
from bot.services.product_mode import install_product_mode
from bot.services.product_mode_overrides import install_product_mode_overrides
from bot.services.product_mode_spa import install_product_mode_spa
from bot.services.vault_manager import VaultManager

from bot.handlers import (
    register_bulk,
    register_errors,
    register_events,
    register_inbox,
    register_nav,
    register_onboarding,
    register_pending_actions,
    register_projects,
    register_reminders,
    register_system,
    register_tasks,
    register_team,
    register_wizards,
)


def build_core(
    *,
    bot_token: str,
    admin_id: int,
    tz_name: str,
    google_client_id: str,
    google_client_secret: str,
    google_refresh_token: str,
    icloud_apple_id: str,
    icloud_app_password: str,
):
    """Create bot, dispatcher and integrations.

    Returns:
        (bot, dp, cloud, vault, gtasks, icloud, llm)

    Side effects:
        Stores a single dependency container under ``dp.workflow_data['deps']``.
    """
    install_product_mode()
    install_product_mode_overrides()
    install_product_mode_spa()
    install_onboarding_import()

    bot = Bot(token=bot_token)

    dp = Dispatcher()
    dp.update.outer_middleware.register(ProcessedUpdateMiddleware())
    guard = SingleUserGuardMiddleware(admin_id=admin_id)
    dp.message.outer_middleware.register(guard)
    dp.callback_query.outer_middleware.register(guard)

    fsm_persistence = FsmPersistenceMiddleware()
    dp.message.middleware.register(fsm_persistence)
    dp.callback_query.middleware.register(fsm_persistence)
    register_nav(dp)
    register_projects(dp)
    register_tasks(dp)
    register_inbox(dp)
    register_bulk(dp)
    register_wizards(dp)
    register_events(dp)
    register_team(dp)
    register_reminders(dp)
    # Must precede the catch-all freeform handlers in register_system.
    register_onboarding(dp)
    register_system(dp)
    register_pending_actions(dp)
    register_errors(dp)

    cloud = WebDavAdapter()
    vault = VaultManager(cloud, tz=tz_name)

    gtasks = GoogleTasksAdapter(
        client_id=google_client_id,
        client_secret=google_client_secret,
        refresh_token=google_refresh_token,
    )

    icloud = ICloudCalDAVAdapter(
        ICloudCalDAVAuth(apple_id=icloud_apple_id, app_password=icloud_app_password)
    )

    gemini = GeminiAdapter(
        api_key=os.getenv("GEMINI_API_KEY", "") or os.getenv("GOOGLE_API_KEY", ""),
        base_url=os.getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta"),
        llm_model=os.getenv("GEMINI_LLM_MODEL", "gemini-3.1-flash-lite-preview"),
        transcribe_model=os.getenv("GEMINI_TRANSCRIBE_MODEL", "gemini-3.1-flash-lite-preview"),
        timeout_sec=int(os.getenv("GEMINI_TIMEOUT_SEC", "45")),
    )
    deepseek = DeepSeekAdapter(
        api_key=os.getenv("DEEPSEEK_API_KEY", ""),
        base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        model=os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
        timeout_sec=int(os.getenv("DEEPSEEK_TIMEOUT_SEC", "45")),
        user_id=os.getenv("DEEPSEEK_USER_ID", f"telegram-{int(admin_id or 0)}"),
    )
    llm = ResilientLLMAdapter(gemini=gemini, deepseek=deepseek)

    deps = AppDeps(
        admin_id=int(admin_id or 0),
        tz_name=tz_name,
        cloud=cloud,
        vault=vault,
        gtasks=gtasks,
        icloud=icloud,
        llm=llm,
        config=load_config(),
    )

    try:
        dp.workflow_data["deps"] = deps
    except Exception:
        pass

    return bot, dp, cloud, vault, gtasks, icloud, llm
