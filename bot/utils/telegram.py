"""Telegram-specific helpers.

These helpers are used throughout the project to reduce chat spam and keep SPA
behaviour consistent.
"""

from __future__ import annotations

import asyncio
import html
import re

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.fsm.context import FSMContext
from aiogram.types import Message


TG_TEXT_LIMIT_UNITS = 4096
TG_TEXT_SAFE_MARGIN_UNITS = 64
TG_TEXT_SAFE_LIMIT_UNITS = TG_TEXT_LIMIT_UNITS - TG_TEXT_SAFE_MARGIN_UNITS


def _tg_utf16_units(text: str) -> int:
    """Telegram counts message length in UTF-16 code units."""
    if not text:
        return 0
    return len(text.encode("utf-16-le")) // 2


def _trim_to_units(text: str, max_units: int) -> str:
    if max_units <= 0 or not text:
        return ""
    if _tg_utf16_units(text) <= max_units:
        return text
    lo = 0
    hi = len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _tg_utf16_units(text[:mid]) <= max_units:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo]


def fit_telegram_text(text: str, *, parse_mode: str | None = None, max_units: int = TG_TEXT_SAFE_LIMIT_UNITS) -> str:
    """Trim text to Telegram limits (by UTF-16 units), preserving line boundaries.

    We compact by lines to reduce the chance of producing invalid HTML parse_mode
    (most screens keep tags within a single line).
    """
    text = str(text or "")
    try:
        max_units = int(max_units)
    except Exception:
        max_units = TG_TEXT_SAFE_LIMIT_UNITS

    if _tg_utf16_units(text) <= max_units:
        return text

    is_html = bool((parse_mode or "").upper() == "HTML")
    lines = text.splitlines()

    suffix_tpl = "\n\n<i>… и ещё {n} строк</i>" if is_html else "\n\n… и ещё {n} строк"

    # Prefer keeping the start of the message and compacting the tail.
    total = len(lines)
    for keep in range(max(1, total - 1), 0, -1):
        hidden = total - keep
        if hidden <= 0:
            continue
        candidate = "\n".join(lines[:keep]) + suffix_tpl.format(n=hidden)
        if _tg_utf16_units(candidate) <= max_units:
            return candidate

    # Edge case: first line itself is too long; fall back to safe plain text.
    plain = re.sub(r"<[^>]+>", "", lines[0] if lines else text)
    plain = plain.strip() or "…"
    if is_html:
        plain = html.escape(plain)
    # Reserve space for the ellipsis.
    cut = _trim_to_units(plain, max(1, max_units - 1))
    return cut + "…"


async def safe_edit(msg: Message, text: str, reply_markup=None, *, parse_mode: str | None = None) -> None:
    """Edit message when possible; fallback to sending a new message."""
    db_pool = getattr(msg.bot, "db_pool", None)
    if db_pool is not None:
        try:
            from bot.ui.render import ui_render

            await ui_render(
                bot=msg.bot,
                db_pool=db_pool,
                chat_id=int(msg.chat.id),
                text=text,
                reply_markup=reply_markup,
                screen=None,
                payload=None,
                fallback_message=msg,
                parse_mode=parse_mode,
            )
            return
        except Exception:
            pass

    text = fit_telegram_text(text, parse_mode=parse_mode)
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


async def safe_edit_by_id(
    *,
    bot: Bot,
    chat_id: int,
    message_id: int,
    text: str,
    reply_markup=None,
    parse_mode: str | None = None,
) -> None:
    """Edit a bot message by id; fallback to sending a new message."""
    db_pool = getattr(bot, "db_pool", None)
    if db_pool is not None:
        try:
            from bot.ui.render import ui_render
            from bot.ui.state import ui_set_state

            async with db_pool.acquire() as conn:
                await ui_set_state(conn, int(chat_id), ui_message_id=int(message_id))
            await ui_render(
                bot=bot,
                db_pool=db_pool,
                chat_id=int(chat_id),
                text=text,
                reply_markup=reply_markup,
                screen=None,
                payload=None,
                fallback_message=None,
                parse_mode=parse_mode,
            )
            return
        except Exception:
            pass

    text = fit_telegram_text(text, parse_mode=parse_mode)
    for _ in range(3):
        try:
            await bot.edit_message_text(
                chat_id=int(chat_id),
                message_id=int(message_id),
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
                disable_web_page_preview=True,
            )
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
            await bot.send_message(
                int(chat_id),
                text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
                disable_web_page_preview=True,
            )
            return
        except TelegramRetryAfter as e:
            await asyncio.sleep(float(getattr(e, "retry_after", 1.0)) + 0.1)
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
    db_pool = getattr(bot, "db_pool", None)
    if db_pool is not None:
        try:
            from bot.ui.render import ui_wizard_render

            await ui_wizard_render(
                bot=bot,
                state=state,
                db_pool=db_pool,
                chat_id=int(chat_id),
                fallback_msg=fallback_msg,
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )
            return
        except Exception:
            pass

    data = await state.get_data()
    wiz_chat_id = int(data.get("wizard_chat_id") or chat_id)
    wiz_msg_id = data.get("wizard_msg_id")
    text = fit_telegram_text(text, parse_mode=parse_mode)

    async def _send_new() -> None:
        sent = await bot.send_message(
            wiz_chat_id,
            text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            disable_web_page_preview=True,
        )
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
                disable_web_page_preview=True,
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
