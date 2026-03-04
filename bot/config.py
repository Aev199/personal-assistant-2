"""
Configuration management for the Telegram task manager bot.

This module handles loading and validating configuration from environment variables.
"""

import os
from dataclasses import dataclass, field
from typing import Optional, Literal

from bot.tz import resolve_tz_name


@dataclass
class BotConfig:
    """Core bot configuration.

    webhook_url is optional because some deployments derive it from
    provider-specific env vars (e.g. Render's RENDER_EXTERNAL_URL).
    """
    token: str
    admin_id: int = 0
    webhook_url: str = ""
    timezone: str = "Europe/Moscow"


@dataclass
class DatabaseConfig:
    """Database configuration."""
    url: str


@dataclass
class GoogleTasksConfig:
    """Google Tasks integration configuration."""
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    refresh_token: Optional[str] = None
    
    @property
    def enabled(self) -> bool:
        """Check if Google Tasks integration is configured."""
        return all([self.client_id, self.client_secret, self.refresh_token])


@dataclass
class ICloudConfig:
    """iCloud Calendar integration configuration."""
    apple_id: Optional[str] = None
    app_password: Optional[str] = None
    calendar_url_work: Optional[str] = None
    calendar_url_personal: Optional[str] = None
    
    @property
    def enabled(self) -> bool:
        """Check if iCloud Calendar integration is configured."""
        return all([self.apple_id, self.app_password])


@dataclass
class WebDAVConfig:
    """WebDAV/Obsidian integration configuration."""
    base_url: Optional[str] = None
    login: Optional[str] = None
    password: Optional[str] = None
    vault_path: Optional[str] = None
    timeout_sec: int = 10
    retries: int = 4
    backoff_sec: float = 0.5
    
    @property
    def enabled(self) -> bool:
        """Check if WebDAV integration is configured."""
        return all([self.base_url, self.login, self.password, self.vault_path])


@dataclass
class BackupConfig:
    """Backup service configuration."""
    storage_backend: Literal["s3", "dropbox", "gcs"] = "s3"
    retention_days: int = 30
    schedule: str = "0 3 * * *"  # Daily at 3:00 AM
    
    # S3 configuration
    aws_access_key_id: Optional[str] = None
    aws_secret_access_key: Optional[str] = None
    aws_s3_bucket: Optional[str] = None
    aws_s3_region: str = "us-east-1"
    
    # Dropbox configuration
    dropbox_access_token: Optional[str] = None
    dropbox_backup_path: str = "/taskbot-backups"
    
    # GCS configuration
    gcs_project_id: Optional[str] = None
    gcs_bucket: Optional[str] = None
    gcs_credentials_json: Optional[str] = None
    
    @property
    def enabled(self) -> bool:
        """Check if backup is configured."""
        if self.storage_backend == "s3":
            return all([self.aws_access_key_id, self.aws_secret_access_key, self.aws_s3_bucket])
        elif self.storage_backend == "dropbox":
            return self.dropbox_access_token is not None
        elif self.storage_backend == "gcs":
            return all([self.gcs_project_id, self.gcs_bucket, self.gcs_credentials_json])
        return False


@dataclass
class LoggingConfig:
    """Logging configuration."""
    level: str = "INFO"
    format: str = "json"
    sample_rate: float = 1.0


@dataclass
class HealthCheckConfig:
    """Health check configuration."""
    timeout: int = 2
    cache_ttl: int = 30
    detailed_metrics: bool = True


@dataclass
class ErrorHandlerConfig:
    """Error handler configuration."""
    notify_user: bool = True
    notify_admin: bool = True
    rate_limit: int = 5  # Max notifications per user per minute
    admin_threshold: int = 10  # Errors per hour before admin alert


@dataclass
class Config:
    """Complete application configuration."""
    bot: BotConfig
    database: DatabaseConfig
    google_tasks: GoogleTasksConfig
    icloud: ICloudConfig
    webdav: WebDAVConfig
    backup: BackupConfig
    logging: LoggingConfig
    health_check: HealthCheckConfig
    error_handler: ErrorHandlerConfig


def load_config() -> Config:
    """Load configuration from environment variables.
    
    Returns:
        Config instance with all configuration loaded
        
    Raises:
        ValueError: If required environment variables are missing
    """
    # Required bot configuration
    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token:
        raise ValueError("BOT_TOKEN environment variable is required")
    
    admin_id_str = os.getenv("ADMIN_ID", "0")

    # Webhook URL is optional: support Render's RENDER_EXTERNAL_URL fallback.
    webhook_url = os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL", "")
    
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise ValueError("DATABASE_URL environment variable is required")
    
    # Bot configuration
    bot_config = BotConfig(
        token=bot_token,
        admin_id=int(admin_id_str or "0"),
        webhook_url=webhook_url,
        # Prefer explicit app timezone variables to avoid provider defaults (TZ=UTC).
        timezone=resolve_tz_name("Europe/Moscow"),
    )
    
    # Database configuration
    database_config = DatabaseConfig(url=database_url)
    
    # Google Tasks configuration (optional)
    google_tasks_config = GoogleTasksConfig(
        client_id=os.getenv("GOOGLE_CLIENT_ID"),
        client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
        refresh_token=os.getenv("GOOGLE_REFRESH_TOKEN"),
    )
    
    # iCloud configuration (optional)
    icloud_config = ICloudConfig(
        apple_id=os.getenv("ICLOUD_APPLE_ID"),
        app_password=os.getenv("ICLOUD_APP_PASSWORD"),
        calendar_url_work=os.getenv("ICLOUD_CALENDAR_URL_WORK"),
        calendar_url_personal=os.getenv("ICLOUD_CALENDAR_URL_PERSONAL"),
    )
    
    # WebDAV configuration (optional)
    webdav_config = WebDAVConfig(
        base_url=os.getenv("WEBDAV_BASE_URL"),
        login=os.getenv("YANDEX_LOGIN"),
        password=os.getenv("YANDEX_PASSWORD"),
        vault_path=os.getenv("VAULT_PATH"),
        timeout_sec=int(os.getenv("WEBDAV_TIMEOUT_SEC", "10")),
        retries=int(os.getenv("WEBDAV_RETRIES", "4")),
        backoff_sec=float(os.getenv("WEBDAV_BACKOFF_SEC", "0.5")),
    )
    
    # Backup configuration (optional)
    backup_config = BackupConfig(
        storage_backend=os.getenv("BACKUP_STORAGE_BACKEND", "s3"),  # type: ignore
        retention_days=int(os.getenv("BACKUP_RETENTION_DAYS", "30")),
        schedule=os.getenv("BACKUP_SCHEDULE", "0 3 * * *"),
        # S3
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
        aws_s3_bucket=os.getenv("AWS_S3_BUCKET"),
        aws_s3_region=os.getenv("AWS_S3_REGION", "us-east-1"),
        # Dropbox
        dropbox_access_token=os.getenv("DROPBOX_ACCESS_TOKEN"),
        dropbox_backup_path=os.getenv("DROPBOX_BACKUP_PATH", "/taskbot-backups"),
        # GCS
        gcs_project_id=os.getenv("GCS_PROJECT_ID"),
        gcs_bucket=os.getenv("GCS_BUCKET"),
        gcs_credentials_json=os.getenv("GCS_CREDENTIALS_JSON"),
    )
    
    # Logging configuration
    logging_config = LoggingConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format=os.getenv("LOG_FORMAT", "json"),
        sample_rate=float(os.getenv("LOG_SAMPLE_RATE", "1.0")),
    )
    
    # Health check configuration
    health_check_config = HealthCheckConfig(
        timeout=int(os.getenv("HEALTH_CHECK_TIMEOUT", "2")),
        cache_ttl=int(os.getenv("HEALTH_CHECK_CACHE_TTL", "30")),
        detailed_metrics=os.getenv("HEALTH_DETAILED_METRICS", "true").lower() == "true",
    )
    
    # Error handler configuration
    error_handler_config = ErrorHandlerConfig(
        notify_user=os.getenv("ERROR_NOTIFY_USER", "true").lower() == "true",
        notify_admin=os.getenv("ERROR_NOTIFY_ADMIN", "true").lower() == "true",
        rate_limit=int(os.getenv("ERROR_RATE_LIMIT", "5")),
        admin_threshold=int(os.getenv("ERROR_ADMIN_THRESHOLD", "10")),
    )
    
    return Config(
        bot=bot_config,
        database=database_config,
        google_tasks=google_tasks_config,
        icloud=icloud_config,
        webdav=webdav_config,
        backup=backup_config,
        logging=logging_config,
        health_check=health_check_config,
        error_handler=error_handler_config,
    )
