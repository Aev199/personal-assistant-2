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


async def get_wizard_message_data(
    state: FSMContext,
    *,
    fallback_chat_id: int | None = None,
) -> tuple[int | None, int | None]:
    try:
        data = await state.get_data()
        wizard_msg_id = data.get("wizard_msg_id")
        if not wizard_msg_id:
            return None, None
        wizard_chat_id = int(data.get("wizard_chat_id") or (fallback_chat_id or 0) or 0)
        return (wizard_chat_id or None), int(wizard_msg_id)
    except Exception:
        return None, None


def split_wizard_message_target(
    wizard_msg_id: int | None,
    *,
    current_message_id: int | None = None,
    prefer_wizard: bool = False,
) -> tuple[int | None, int | None]:
    if not wizard_msg_id:
        return None, None
    wiz_msg_id = int(wizard_msg_id)
    if prefer_wizard:
        return wiz_msg_id, None
    if current_message_id and int(current_message_id) == wiz_msg_id:
        return wiz_msg_id, None
    return None, wiz_msg_id


async def cleanup_stale_wizard_message(
    bot,
    *,
    chat_id: int | None,
    stale_message_id: int | None,
    final_message_id: int | None,
) -> None:
    if not chat_id or not stale_message_id:
        return
    if not final_message_id:
        return
    if final_message_id and int(final_message_id) == int(stale_message_id):
        return
    try:
        await bot.delete_message(chat_id=int(chat_id), message_id=int(stale_message_id))
    except Exception:
        return


async def escape_hatch_menu_or_command(message: Message, state: FSMContext, db_pool: asyncpg.Pool) -> bool:
    """Allow main menu texts and core commands to work during FSM wizards.

    Returns True if it handled the message.
    """

    if not message.text:
        return False

    raw = message.text.strip()
    wizard_chat_id, wizard_msg_id = await get_wizard_message_data(
        state,
        fallback_chat_id=int(message.chat.id),
    )
    preferred_message_id, stale_wizard_msg_id = split_wizard_message_target(
        wizard_msg_id,
        prefer_wizard=True,
    )

    if raw.startswith("/help"):
        await state.clear()
        await try_delete_user_message(message)
        final_id = await ui_render_help(
            message,
            db_pool,
            preferred_message_id=preferred_message_id,
            force_new=False,
        )
        await cleanup_stale_wizard_message(
            message.bot,
            chat_id=wizard_chat_id,
            stale_message_id=stale_wizard_msg_id,
            final_message_id=final_id,
        )
        return True

    if raw.startswith("/start") or raw.startswith("/menu"):
        await state.clear()
        await try_delete_user_message(message)
        final_id = await ui_render_home(
            message,
            db_pool,
            preferred_message_id=preferred_message_id,
            force_new=False,
        )
        await cleanup_stale_wizard_message(
            message.bot,
            chat_id=wizard_chat_id,
            stale_message_id=stale_wizard_msg_id,
            final_message_id=final_id,
        )
        return True

    if raw.startswith("/"):
        await state.clear()
        await try_delete_user_message(message)
        final_id = await ui_render_home(
            message,
            db_pool,
            preferred_message_id=preferred_message_id,
            force_new=False,
        )
        await cleanup_stale_wizard_message(
            message.bot,
            chat_id=wizard_chat_id,
            stale_message_id=stale_wizard_msg_id,
            final_message_id=final_id,
        )
        return True

    token = canon(raw)
    if token not in MAIN_MENU_TOKENS:
        return False

    await state.clear()
    await try_delete_user_message(message)

    if token == "проекты":
        final_id = await ui_render_projects_portfolio(
            message,
            db_pool,
            preferred_message_id=preferred_message_id,
            force_new=False,
        )
    elif token == "сегодня":
        final_id = await ui_render_today(
            message,
            db_pool,
            preferred_message_id=preferred_message_id,
            force_new=False,
        )
    elif token == "просрочки":
        final_id = await ui_render_overdue(
            message,
            db_pool,
            preferred_message_id=preferred_message_id,
            force_new=False,
        )
    elif token == "добавить":
        final_id = await ui_render_add_menu(
            message,
            db_pool,
            preferred_message_id=preferred_message_id,
            force_new=False,
        )
    elif token == "help":
        final_id = await ui_render_help(
            message,
            db_pool,
            preferred_message_id=preferred_message_id,
            force_new=False,
        )
    else:
        final_id = await ui_render_team(
            message,
            db_pool,
            preferred_message_id=preferred_message_id,
            force_new=False,
        )

    await cleanup_stale_wizard_message(
        message.bot,
        chat_id=wizard_chat_id,
        stale_message_id=stale_wizard_msg_id,
        final_message_id=final_id,
    )
    return True
