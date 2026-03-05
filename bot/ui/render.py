"""SPA UI renderer.

The Ultimate SPA design keeps a single editable UI message per chat.
This renderer edits the stored ui_message_id when possible; otherwise it sends
a new message and stores its id.
"""

from __future__ import annotations

import asyncio
import logging

import asyncpg
from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup, Message

from bot.ui.state import ui_get_state, ui_set_state
from bot.utils.telegram import fit_telegram_text

logger = logging.getLogger(__name__)


def _is_editable_fallback(msg: Message | None) -> bool:
    if msg is None:
        return False
    try:
        return bool(msg.from_user and msg.from_user.is_bot)
    except Exception:
        return False


def _pick_edit_targets(
    old_ui_id: int | None,
    preferred_message_id: int | None,
    fallback_is_editable: bool,
    fallback_id: int | None,
    force_new: bool,
) -> list[int]:
    if force_new:
        return []

    targets: list[int] = []
    if preferred_message_id:
        targets.append(int(preferred_message_id))
    if old_ui_id and int(old_ui_id) not in targets:
        targets.append(int(old_ui_id))
    if not old_ui_id and fallback_is_editable and fallback_id and int(fallback_id) not in targets:
        targets.append(int(fallback_id))
    return targets


async def ui_render(
    *,
    bot: Bot,
    db_pool: asyncpg.Pool,
    chat_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup | None,
    screen: str | None = None,
    payload: dict | None = None,
    fallback_message: Message | None = None,
    preferred_message_id: int | None = None,
    force_new: bool = False,
    parse_mode: str | None = "HTML",
) -> int:
    """Render/update the single UI message for a chat.

    Behaviour:
    - tries to edit stored ui_message_id (unless force_new)
    - if edit fails, sends a new message, stores its id, and best-effort deletes old UI message
    - always updates ui_screen + ui_payload in user_settings

    Notes:
    - payload can be {} to explicitly clear payload; do not use `payload or ...`
    """
    text = fit_telegram_text(text, parse_mode=parse_mode)
    # Load current UI state once.
    async with db_pool.acquire() as conn:
        state = await ui_get_state(conn, chat_id)
        old_ui_msg_id = state.get("ui_message_id")
        existing_payload = state.get("ui_payload") or {}
        existing_screen = str(state.get("ui_screen") or "home")

    new_payload: dict = existing_payload
    if payload is not None:
        new_payload = payload
    new_screen = str(screen or existing_screen or "home")
    fallback_id = getattr(fallback_message, "message_id", None)
    preferred_id = int(preferred_message_id) if preferred_message_id else None
    edit_targets = _pick_edit_targets(
        int(old_ui_msg_id) if old_ui_msg_id else None,
        preferred_id,
        _is_editable_fallback(fallback_message),
        int(fallback_id) if fallback_id else None,
        force_new,
    )

    async def _persist(*, ui_message_id: int | None = None) -> None:
        async with db_pool.acquire() as conn:
            await ui_set_state(
                conn,
                chat_id,
                ui_message_id=ui_message_id,
                ui_screen=new_screen,
                ui_payload=new_payload,
            )

    async def _cleanup_after_success(final_message_id: int) -> None:
        cleanup_ids: list[int] = []
        if old_ui_msg_id and int(old_ui_msg_id) != int(final_message_id):
            cleanup_ids.append(int(old_ui_msg_id))
        if preferred_id and int(preferred_id) != int(final_message_id):
            if not old_ui_msg_id or int(preferred_id) != int(old_ui_msg_id):
                cleanup_ids.append(int(preferred_id))
        for message_id in cleanup_ids:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=int(message_id))
            except Exception:
                pass

    # Try edit candidate UI messages.
    for ui_msg_id in edit_targets:
        for _ in range(3):
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=int(ui_msg_id),
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode,
                    disable_web_page_preview=True,
                )
                await _persist(ui_message_id=int(ui_msg_id))
                await _cleanup_after_success(int(ui_msg_id))
                return int(ui_msg_id)
            except TelegramRetryAfter as e:
                await asyncio.sleep(float(getattr(e, "retry_after", 1.0)) + 0.1)
            except TelegramBadRequest as e:
                # Treat "not modified" as success but still persist state.
                if "message is not modified" in str(e).lower():
                    await _persist(ui_message_id=int(ui_msg_id))
                    await _cleanup_after_success(int(ui_msg_id))
                    return int(ui_msg_id)
                break
            except Exception:
                break

    # Send new UI message.
    sent = None
    send_failed = False
    for _ in range(3):
        try:
            sent = await bot.send_message(
                chat_id,
                text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
                disable_web_page_preview=True,
            )
            break
        except TelegramRetryAfter as e:
            await asyncio.sleep(float(getattr(e, "retry_after", 1.0)) + 0.1)
        except Exception:
            send_failed = True
            logger.exception("ui_render send failed", extra={"chat_id": int(chat_id)})
            break

    if not sent:
        if not send_failed:
            logger.exception(
                "ui_render send did not produce a message",
                extra={"chat_id": int(chat_id)},
            )
        return 0

    await _persist(ui_message_id=int(sent.message_id))
    await _cleanup_after_success(int(sent.message_id))
    return int(sent.message_id)


async def ui_wizard_render(
    *,
    bot: Bot,
    state: FSMContext,
    db_pool: asyncpg.Pool,
    chat_id: int,
    fallback_msg: Message | None,
    text: str,
    reply_markup=None,
    parse_mode: str | None = None,
) -> int:
    data = await state.get_data()
    preferred_message_id = data.get("wizard_msg_id")
    message_id = await ui_render(
        bot=bot,
        db_pool=db_pool,
        chat_id=int(chat_id),
        text=text,
        reply_markup=reply_markup,
        screen=None,
        payload=None,
        fallback_message=fallback_msg,
        preferred_message_id=int(preferred_message_id) if preferred_message_id else None,
        force_new=False,
        parse_mode=parse_mode,
    )
    if message_id > 0:
        await state.update_data(
            wizard_chat_id=int(chat_id),
            wizard_msg_id=int(message_id),
        )
    return int(message_id)


def _bot_db_pool(bot: Bot) -> asyncpg.Pool:
    db_pool = getattr(bot, "db_pool", None)
    if db_pool is None:
        raise RuntimeError("Bot db_pool is not configured")
    return db_pool


async def ui_safe_edit(
    msg: Message,
    text: str,
    reply_markup=None,
    *,
    parse_mode: str | None = None,
) -> int:
    return await ui_render(
        bot=msg.bot,
        db_pool=_bot_db_pool(msg.bot),
        chat_id=int(msg.chat.id),
        text=text,
        reply_markup=reply_markup,
        screen=None,
        payload=None,
        fallback_message=msg,
        parse_mode=parse_mode,
    )


async def ui_safe_wizard_render(
    *,
    bot: Bot,
    state: FSMContext,
    chat_id: int,
    fallback_msg: Message | None,
    text: str,
    reply_markup=None,
    parse_mode: str | None = None,
) -> int:
    return await ui_wizard_render(
        bot=bot,
        state=state,
        db_pool=_bot_db_pool(bot),
        chat_id=int(chat_id),
        fallback_msg=fallback_msg,
        text=text,
        reply_markup=reply_markup,
        parse_mode=parse_mode,
    )
