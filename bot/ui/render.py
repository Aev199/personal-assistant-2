"""SPA UI renderer.

The Ultimate SPA design keeps a single editable UI message per chat.
This renderer edits the stored ui_message_id when possible; otherwise it sends
a new message and stores its id.
"""

from __future__ import annotations

import asyncio

import asyncpg
from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup, Message

from bot.ui.state import ui_get_state, ui_set_state
from bot.utils.telegram import fit_telegram_text


async def ui_adopt_message(
    *,
    bot: Bot,
    db_pool: asyncpg.Pool,
    chat_id: int,
    message_id: int,
    delete_old: bool = True,
) -> None:
    """Adopt `message_id` as the SPA UI message for the chat.

    If requested, best-effort delete the previously stored UI message to keep chat clean.
    Screen/payload are left intact.
    """
    chat_id = int(chat_id)
    message_id = int(message_id)
    async with db_pool.acquire() as conn:
        state = await ui_get_state(conn, chat_id)
        old_id = state.get("ui_message_id")

    if delete_old and old_id and int(old_id) != int(message_id):
        try:
            await bot.delete_message(chat_id=chat_id, message_id=int(old_id))
        except Exception:
            pass

    async with db_pool.acquire() as conn:
        await ui_set_state(conn, chat_id, ui_message_id=message_id)


def _is_editable_fallback(msg: Message | None) -> bool:
    if msg is None:
        return False
    try:
        return bool(msg.from_user and msg.from_user.is_bot)
    except Exception:
        return False


def _pick_edit_targets(
    old_ui_id: int | None,
    fallback_is_editable: bool,
    fallback_id: int | None,
    force_new: bool,
) -> list[int]:
    if force_new:
        return []

    targets: list[int] = []
    if fallback_is_editable and fallback_id:
        targets.append(int(fallback_id))
    if old_ui_id and int(old_ui_id) not in targets:
        targets.append(int(old_ui_id))
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
    edit_targets = _pick_edit_targets(
        int(old_ui_msg_id) if old_ui_msg_id else None,
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

    # Try edit candidate UI messages. Prefer the message the user just interacted with.
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
                if old_ui_msg_id and int(old_ui_msg_id) != int(ui_msg_id):
                    try:
                        await bot.delete_message(chat_id=chat_id, message_id=int(old_ui_msg_id))
                    except Exception:
                        pass
                return int(ui_msg_id)
            except TelegramRetryAfter as e:
                await asyncio.sleep(float(getattr(e, "retry_after", 1.0)) + 0.1)
            except TelegramBadRequest as e:
                # Treat "not modified" as success but still persist state.
                if "message is not modified" in str(e).lower():
                    await _persist(ui_message_id=int(ui_msg_id))
                    if old_ui_msg_id and int(old_ui_msg_id) != int(ui_msg_id):
                        try:
                            await bot.delete_message(chat_id=chat_id, message_id=int(old_ui_msg_id))
                        except Exception:
                            pass
                    return int(ui_msg_id)
                break
            except Exception:
                break

    # Send new UI message.
    sent = None
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
            return int(old_ui_msg_id or fallback_id or 0)

    if not sent:
        return int(old_ui_msg_id or fallback_id or 0)

    # Best-effort delete old UI message to keep chat clean.
    if old_ui_msg_id and int(old_ui_msg_id) != int(sent.message_id):
        try:
            await bot.delete_message(chat_id=chat_id, message_id=int(old_ui_msg_id))
        except Exception:
            pass

    await _persist(ui_message_id=int(sent.message_id))
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
    message_id = await ui_render(
        bot=bot,
        db_pool=db_pool,
        chat_id=int(chat_id),
        text=text,
        reply_markup=reply_markup,
        screen=None,
        payload=None,
        fallback_message=fallback_msg,
        force_new=False,
        parse_mode=parse_mode,
    )
    if message_id > 0:
        await state.update_data(
            wizard_chat_id=int(chat_id),
            wizard_msg_id=int(message_id),
        )
    return int(message_id)
