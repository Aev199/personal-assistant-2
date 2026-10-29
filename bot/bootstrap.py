"""Create the Telegram bot, dispatcher, integrations and the shared deps container."""

from __future__ import annotations

import os

from aiogram import Bot, Dispatcher

from bot.adapters.deepseek_adapter import DeepSeekAdapter
from bot.adapters.gemini_adapter import GeminiAdapter
from bot.adapters.icloud_caldav_adapter import ICloudCalDAVAdapter, ICloudCalDAVAuth
from bot.adapters.llm_router import ResilientLLMAdapter
from bot.config import load_config
from bot.deps import AppDeps
from bot.handlers.errors import register as register_errors
from bot.middlewares.guards import ProcessedUpdateMiddleware, SingleUserGuardMiddleware
from bot.tg import register as register_telegram
from bot.ui.simple_daily import set_default_icloud


def build_core(
    *,
    bot_token: str,
    admin_id: int,
    tz_name: str,
    icloud_apple_id: str,
    icloud_app_password: str,
):
    """Create bot, dispatcher and integrations.

    Returns:
        (bot, dp, icloud, llm)

    Side effects:
        Stores a single dependency container under ``dp.workflow_data['deps']``.
    """
    bot = Bot(token=bot_token)

    dp = Dispatcher()
    dp.update.outer_middleware.register(ProcessedUpdateMiddleware())
    guard = SingleUserGuardMiddleware(admin_id=admin_id)
    dp.message.outer_middleware.register(guard)
    dp.callback_query.outer_middleware.register(guard)
    register_telegram(dp)
    register_errors(dp)

    icloud = ICloudCalDAVAdapter(
        ICloudCalDAVAuth(apple_id=icloud_apple_id, app_password=icloud_app_password)
    )
    set_default_icloud(icloud)

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
        max_tokens=int(os.getenv("DEEPSEEK_MAX_TOKENS", "8192")),
        user_id=os.getenv("DEEPSEEK_USER_ID", f"telegram-{int(admin_id or 0)}"),
    )
    llm = ResilientLLMAdapter(gemini=gemini, deepseek=deepseek)

    deps = AppDeps(
        admin_id=int(admin_id or 0),
        tz_name=tz_name,
        icloud=icloud,
        llm=llm,
        config=load_config(),
    )

    dp.workflow_data["deps"] = deps
    return bot, dp, icloud, llm
