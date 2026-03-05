"""Common handler helpers.

Provides the "escape hatch" so bottom menu and core commands work even inside FSM wizards.
"""

from __future__ import annotations

import asyncpg
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot.ui.screens import (
    ui_render_add_menu,
    ui_render_help,
    ui_render_home,
    ui_render_overdue,
    ui_render_projects_portfolio,
    ui_render_team,
    ui_render_today,
)
from bot.utils import canon, try_delete_user_message


MAIN_MENU_TOKENS = {
    "сегодня",
    "проекты",
    "просрочки",
    "команда",
    "добавить",
    "help",
}


async def escape_hatch_menu_or_command(message: Message, state: FSMContext, db_pool: asyncpg.Pool) -> bool:
    """Allow main menu texts and core commands to work during FSM wizards.

    Returns True if it handled the message.
    """

    if not message.text:
        return False

    raw = message.text.strip()

    # Commands
    if raw.startswith("/help"):
        await state.clear()
        await try_delete_user_message(message)
        await ui_render_help(message, db_pool, force_new=True)
        return True

    if raw.startswith("/start") or raw.startswith("/menu"):
        await state.clear()
        await try_delete_user_message(message)
        await ui_render_home(message, db_pool, force_new=True)
        return True

    if raw.startswith("/"):
        # Unknown command
        await state.clear()
        await try_delete_user_message(message)
        await ui_render_home(message, db_pool, force_new=True)
        return True

    token = canon(raw)
    if token not in MAIN_MENU_TOKENS:
        return False

    await state.clear()
    await try_delete_user_message(message)

    if token == "проекты":
        await ui_render_projects_portfolio(message, db_pool, force_new=True)
    elif token == "сегодня":
        await ui_render_today(message, db_pool, force_new=True)
    elif token == "просрочки":
        await ui_render_overdue(message, db_pool, force_new=True)
    elif token == "добавить":
        await ui_render_add_menu(message, db_pool, force_new=True)
    elif token == "help":
        await ui_render_help(message, db_pool, force_new=True)
    else:
        await ui_render_team(message, db_pool, force_new=True)

    return True
