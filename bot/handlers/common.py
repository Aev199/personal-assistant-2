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
from bot.ui.render import ui_adopt_message
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
    async def _adopt_wizard_message() -> None:
        try:
            data = await state.get_data()
            wiz_chat_id = int(data.get("wizard_chat_id") or message.chat.id)
            wiz_msg_id = data.get("wizard_msg_id")
            if wiz_msg_id:
                await ui_adopt_message(
                    bot=message.bot,
                    db_pool=db_pool,
                    chat_id=wiz_chat_id,
                    message_id=int(wiz_msg_id),
                    delete_old=True,
                )
        except Exception:
            return

    # Commands
    if raw.startswith("/help"):
        await _adopt_wizard_message()
        await state.clear()
        await try_delete_user_message(message)
        await ui_render_help(message, db_pool, force_new=False)
        return True

    if raw.startswith("/start") or raw.startswith("/menu"):
        await _adopt_wizard_message()
        await state.clear()
        await try_delete_user_message(message)
        await ui_render_home(message, db_pool, force_new=False)
        return True

    if raw.startswith("/"):
        # Unknown command
        await _adopt_wizard_message()
        await state.clear()
        await try_delete_user_message(message)
        await ui_render_home(message, db_pool, force_new=False)
        return True

    token = canon(raw)
    if token not in MAIN_MENU_TOKENS:
        return False

    await _adopt_wizard_message()
    await state.clear()
    await try_delete_user_message(message)

    if token == "проекты":
        await ui_render_projects_portfolio(message, db_pool, force_new=False)
    elif token == "сегодня":
        await ui_render_today(message, db_pool, force_new=False)
    elif token == "просрочки":
        await ui_render_overdue(message, db_pool, force_new=False)
    elif token == "добавить":
        await ui_render_add_menu(message, db_pool, force_new=False)
    elif token == "help":
        await ui_render_help(message, db_pool, force_new=False)
    else:
        await ui_render_team(message, db_pool, force_new=False)

    return True
