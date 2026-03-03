"""Telegram-specific helpers.

These helpers are used throughout the project to reduce chat spam and keep SPA
behaviour consistent.
"""

from __future__ import annotations

import asyncio

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.fsm.context import FSMContext
from aiogram.types import Message


async def safe_edit(msg: Message, text: str, reply_markup=None, *, parse_mode: str | None = None) -> None:
    """Edit message when possible; fallback to sending a new message."""
    for _ in range(3):
        try:
            await msg.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
            return
        except TelegramRetryAfter as e:
            await asyncio.sleep(float(getattr(e, "retry_after", 1.0)) + 0.1)
        except TelegramBadRequest as e:
            # message is not modified — treat as success to avoid chat spam
            if "message is not modified" in str(e).lower():
                return
            break
        except Exception:
            break

    for _ in range(3):
        try:
            await msg.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
            return
        except TelegramRetryAfter as e:
            await asyncio.sleep(float(getattr(e, "retry_after", 1.0)) + 0.1)
        except Exception:
            return


async def try_delete_user_message(message: Message) -> None:
    """Best-effort delete user's message to avoid chat spam."""
    try:
        await message.delete()
    except Exception:
        return


async def wizard_render(
    *,
    bot: Bot,
    state: FSMContext,
    chat_id: int,
    fallback_msg: Message | None,
    text: str,
    reply_markup=None,
    parse_mode: str | None = None,
) -> None:
    """Render wizard UI as a single "screen" message.

    Uses wizard_msg_id from FSM data; if edit fails, sends a new message and
    updates wizard_msg_id.
    """

    data = await state.get_data()
    wiz_chat_id = int(data.get("wizard_chat_id") or chat_id)
    wiz_msg_id = data.get("wizard_msg_id")

    async def _send_new() -> None:
        sent = await bot.send_message(wiz_chat_id, text, reply_markup=reply_markup, parse_mode=parse_mode)
        await state.update_data(wizard_chat_id=wiz_chat_id, wizard_msg_id=sent.message_id)

    if not wiz_msg_id:
        if fallback_msg is not None:
            await state.update_data(wizard_chat_id=wiz_chat_id, wizard_msg_id=fallback_msg.message_id)
            wiz_msg_id = fallback_msg.message_id
        else:
            return await _send_new()

    for _ in range(3):
        try:
            await bot.edit_message_text(
                chat_id=wiz_chat_id,
                message_id=int(wiz_msg_id),
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )
            return
        except TelegramRetryAfter as e:
            await asyncio.sleep(float(getattr(e, "retry_after", 1.0)) + 0.1)
        except TelegramBadRequest as e:
            if "message is not modified" in str(e).lower():
                return
            break
        except Exception:
            break

    for _ in range(3):
        try:
            await _send_new()
            return
        except TelegramRetryAfter as e:
            await asyncio.sleep(float(getattr(e, "retry_after", 1.0)) + 0.1)
        except Exception:
            return
