"""Dependency container for the application."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional, TYPE_CHECKING

import asyncpg

from bot.adapters.google_tasks_adapter import GoogleTasksAdapter
from bot.adapters.icloud_caldav_adapter import ICloudCalDAVAdapter
from bot.adapters.llm_router import ResilientLLMAdapter
from bot.adapters.webdav_adapter import WebDavAdapter
from bot.services.vault_manager import VaultManager

if TYPE_CHECKING:  # pragma: no cover
    from bot.config import Config
    from bot.services.error_handler import ErrorHandler
    from bot.services.logger import StructuredLogger

DbLogErrorFn = Callable[[str, Exception, Optional[dict[str, Any]]], Any]


@dataclass
class AppDeps:
    """Shared dependencies & runtime metadata."""

    admin_id: int
    tz_name: str

    cloud: WebDavAdapter
    vault: VaultManager
    gtasks: GoogleTasksAdapter
    icloud: ICloudCalDAVAdapter
    llm: ResilientLLMAdapter | None = None
    config: Optional["Config"] = None

    db_pool: Optional[asyncpg.Pool] = None
    db_log_error: Optional[DbLogErrorFn] = None

    db_tasks_deadline_timestamptz: bool = False
    db_reminders_remind_at_timestamptz: bool = False
    db_projects_deadline_timestamptz: bool = False

    logger: Optional["StructuredLogger"] = None
    error_handler: Optional["ErrorHandler"] = None

    error_notify_user: bool = True
    error_notify_admin: bool = True

    @property
    def llm_online(self) -> bool:
        """True when at least one text provider is configured and available."""
        if self.llm is None:
            return False
        return bool(self.llm.enabled and not self.llm.circuit_open)
