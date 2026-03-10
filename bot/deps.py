"""Dependency container for the application.

Aiogram can inject arbitrary objects into handler call signatures via
``Dispatcher.workflow_data``. To avoid scattering many separate keys
(``vault``, ``gtasks``, ``icloud``...), we centralize integrations and
runtime metadata in a single object: :class:`AppDeps`.

Handlers can then depend on ``deps: AppDeps`` instead of a growing list of
individually-injected services.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional, TYPE_CHECKING

import asyncpg

from bot.adapters.google_tasks_adapter import GoogleTasksAdapter
from bot.adapters.icloud_caldav_adapter import ICloudCalDAVAdapter
from bot.adapters.gemini_adapter import GeminiAdapter
from bot.adapters.webdav_adapter import WebDavAdapter
from bot.services.vault_manager import VaultManager

if TYPE_CHECKING:  # pragma: no cover
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
    llm: GeminiAdapter | None = None

    # Filled during startup
    db_pool: Optional[asyncpg.Pool] = None
    db_log_error: Optional[DbLogErrorFn] = None

    # DB schema compatibility flags (filled during startup)
    # Some historical deployments used TIMESTAMPTZ for deadline/remind_at.
    db_tasks_deadline_timestamptz: bool = False
    db_reminders_remind_at_timestamptz: bool = False
    db_projects_deadline_timestamptz: bool = False

    # Optional runtime services
    logger: Optional["StructuredLogger"] = None
    error_handler: Optional["ErrorHandler"] = None

    # Error handler policy (filled from config/env)
    error_notify_user: bool = True
    error_notify_admin: bool = True
