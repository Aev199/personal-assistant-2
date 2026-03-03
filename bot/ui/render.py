"""SPA UI renderer.

The Ultimate SPA design keeps a single editable UI message per chat.
This renderer edits the stored ui_message_id when possible; otherwise it sends
a new message and stores its id.
"""

from __future__ import annotations

import asyncio

import asyncpg
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup, Message

from bot.ui.state import ui_get_state, ui_set_state


async def ui_render(
    *,
    bot: Bot,
    db_pool: asyncpg.Pool,
    chat_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup | None,
    screen: str = "home",
    payload: dict | None = None,
    fallback_message: Message | None = None,
    force_new: bool = False,
    parse_mode: str | None = "HTML",
) -> None:
    """Render/update the single UI message for a chat.

    Behaviour:
    - tries to edit stored ui_message_id (unless force_new)
    - if edit fails, sends a new message, stores its id, and best-effort deletes old UI message
    - always updates ui_screen + ui_payload in user_settings

    Notes:
    - payload can be {} to explicitly clear payload; do not use `payload or ...`
    """
    # Load current UI state once.
    async with db_pool.acquire() as conn:
        state = await ui_get_state(conn, chat_id)
        old_ui_msg_id = state.get("ui_message_id")
        ui_msg_id = None if force_new else old_ui_msg_id
        existing_payload = state.get("ui_payload") or {}

    new_payload: dict = existing_payload
    if payload is not None:
        new_payload = payload

    async def _persist(*, ui_message_id: int | None = None) -> None:
        async with db_pool.acquire() as conn:
            await ui_set_state(
                conn,
                chat_id,
                ui_message_id=ui_message_id,
                ui_screen=screen,
                ui_payload=new_payload,
            )

    # Try edit existing UI message.
    if ui_msg_id:
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
                return
            except TelegramRetryAfter as e:
                await asyncio.sleep(float(getattr(e, "retry_after", 1.0)) + 0.1)
            except TelegramBadRequest as e:
                # Treat "not modified" as success but still persist state.
                if "message is not modified" in str(e).lower():
                    await _persist(ui_message_id=int(ui_msg_id))
                    return
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
            return

    if not sent:
        return

    # Best-effort delete old UI message to keep chat clean.
    if old_ui_msg_id:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=int(old_ui_msg_id))
        except Exception:
            pass

    await _persist(ui_message_id=int(sent.message_id))
