"""Configuration from environment variables (see /etc/personal-assistant-bot.env on the VPS)."""

import os
from dataclasses import dataclass

from bot.tz import resolve_tz_name


@dataclass
class BotConfig:
    token: str
    admin_id: int = 0
    internal_api_key: str = ""
    webhook_url: str = ""
    webhook_secret_token: str = ""
    timezone: str = "Europe/Moscow"


@dataclass
class DatabaseConfig:
    url: str


@dataclass
class LoggingConfig:
    level: str = "INFO"
    format: str = "json"


@dataclass
class ErrorHandlerConfig:
    notify_user: bool = True
    notify_admin: bool = True
    rate_limit: int = 5  # Max notifications per user per minute


@dataclass
class Config:
    bot: BotConfig
    database: DatabaseConfig
    logging: LoggingConfig
    error_handler: ErrorHandlerConfig


def _flag(name: str, default: str = "true") -> bool:
    return os.getenv(name, default).lower() == "true"


def load_config() -> Config:
    """Raises ValueError when a required variable is missing."""
    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token:
        raise ValueError("BOT_TOKEN environment variable is required")
    try:
        admin_id = int(os.getenv("ADMIN_ID", "0") or "0")
    except ValueError as exc:
        raise ValueError("ADMIN_ID must be a valid integer") from exc
    if admin_id <= 0:
        raise ValueError("ADMIN_ID environment variable is required and must be > 0")
    internal_api_key = (os.getenv("INTERNAL_API_KEY") or "").strip()
    if not internal_api_key:
        raise ValueError("INTERNAL_API_KEY environment variable is required")
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise ValueError("DATABASE_URL environment variable is required")

    return Config(
        bot=BotConfig(
            token=bot_token,
            admin_id=admin_id,
            internal_api_key=internal_api_key,
            webhook_url=os.getenv("WEBHOOK_URL", ""),
            webhook_secret_token=(
                os.getenv("TELEGRAM_WEBHOOK_SECRET") or os.getenv("WEBHOOK_SECRET_TOKEN") or internal_api_key
            ),
            # Prefers BOT_TIMEZONE / APP_TIMEZONE over a provider default TZ=UTC.
            timezone=resolve_tz_name("Europe/Moscow"),
        ),
        database=DatabaseConfig(url=database_url),
        logging=LoggingConfig(level=os.getenv("LOG_LEVEL", "INFO"), format=os.getenv("LOG_FORMAT", "json")),
        error_handler=ErrorHandlerConfig(
            notify_user=_flag("ERROR_NOTIFY_USER"),
            notify_admin=_flag("ERROR_NOTIFY_ADMIN"),
            rate_limit=int(os.getenv("ERROR_RATE_LIMIT", "5")),
        ),
    )
