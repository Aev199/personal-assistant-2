"""Bootstrap helpers.

This module centralizes creation of the aiogram Bot/Dispatcher and wiring of
external integrations (WebDAV/Obsidian vault, Google Tasks, iCloud CalDAV).

The refactor keeps production runtime in :mod:`bot.runtime` + modular handlers.
Legacy monolith (if ever needed) lives outside the :mod:`bot` package.
"""

from __future__ import annotations

from aiogram import Bot, Dispatcher

from bot.adapters.webdav_adapter import WebDavAdapter
from bot.services.vault_manager import VaultManager
from bot.adapters.google_tasks_adapter import GoogleTasksAdapter
from bot.adapters.icloud_caldav_adapter import ICloudCalDAVAdapter, ICloudCalDAVAuth
from bot.deps import AppDeps

from bot.handlers import (
    register_nav,
    register_projects,
    register_tasks,
    register_inbox,
    register_bulk,
    register_wizards,
    register_events,
    register_team,
    register_reminders,
    register_system,
    register_errors,
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
        (bot, dp, cloud, vault, gtasks, icloud)

    Side effects:
        Stores a single dependency container under ``dp.workflow_data['deps']``.
    """

    bot = Bot(token=bot_token)

    dp = Dispatcher()
    register_nav(dp)
    register_projects(dp)
    register_tasks(dp)
    register_inbox(dp)
    register_bulk(dp)
    register_wizards(dp)
    register_events(dp)
    register_team(dp)
    register_reminders(dp)
    register_system(dp)
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

    deps = AppDeps(
        admin_id=int(admin_id or 0),
        tz_name=tz_name,
        cloud=cloud,
        vault=vault,
        gtasks=gtasks,
        icloud=icloud,
    )

    # Expose deps for DI in handler modules
    try:
        dp.workflow_data["deps"] = deps
    except Exception:
        pass

    return bot, dp, cloud, vault, gtasks, icloud
