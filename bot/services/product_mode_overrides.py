"""Small safety refinements applied after the main product-mode installer."""

from __future__ import annotations

from typing import Any

import asyncpg
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

import bot.services.product_mode as product_mode
from bot.db.runtime_state import forget_recent_action
from bot.utils import h


_original_preview = None
_original_focus_line = None
_original_rerender = None
_installed = False


async def _safe_rerender_with_toast(message: Message, db_pool: asyncpg.Pool, deps, toast: str) -> int:
    """Do not reinterpret a successful write as failed when only SPA refresh fails."""
    if _original_rerender is None:
        return 0
    try:
        return int(await _original_rerender(message, db_pool, deps, toast))
    except Exception:
        try:
            sent = await message.answer(toast)
            return int(sent.message_id)
        except Exception:
            return 0


async def _preview_without_stale_draft_fingerprint(
    message: Message,
    *,
    db_pool: asyncpg.Pool,
    deps,
    kind: str,
    payload: dict[str, Any],
    fingerprint: str,
    summary: str,
    source: str,
    force_new: bool = False,
) -> int:
    if _original_preview is None:
        raise RuntimeError("product mode overrides are not installed")

    pending_action_id = await _original_preview(
        message,
        db_pool=db_pool,
        deps=deps,
        kind=kind,
        payload=payload,
        fingerprint=fingerprint,
        summary=summary,
        source=source,
        force_new=force_new,
    )

    # The legacy duplicate guard is useful for drafts, but an immediately
    # executed action must not later be described as an unconfirmed draft.
    if kind in product_mode.SAFE_INSTANT_KINDS and pending_action_id:
        try:
            async with db_pool.acquire() as conn:
                status = await conn.fetchval(
                    "SELECT status FROM pending_actions WHERE id=$1",
                    int(pending_action_id),
                )
                if str(status or "") == "executed":
                    await forget_recent_action(
                        conn,
                        chat_id=int(message.chat.id),
                        fingerprint=fingerprint,
                    )
        except Exception:
            # Failure to clean a short-lived duplicate marker must not turn a
            # successful capture into an error for the user.
            pass

    return int(pending_action_id)


async def _send_accurate_batch_summary(message: Message, count: int) -> int | None:
    if not product_mode._env_enabled("ASSISTANT_INSTANT_CAPTURE", True):
        text = f"📋 Создано черновиков: {int(count)}. Всё верно?"
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"✅ Подтвердить всё ({int(count)})",
                        callback_data="llm:batch_confirm",
                    )
                ]
            ]
        )
    else:
        text = (
            f"✅ Обработал действий: {int(count)}. "
            "Задачи, идеи и напоминания сохранены сразу; "
            "события подтвердите в карточках выше."
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="📅 Открыть сегодня", callback_data="nav:today"),
                    InlineKeyboardButton(text="📥 Inbox", callback_data="nav:inbox:0"),
                ]
            ]
        )
    try:
        sent = await message.answer(text, reply_markup=keyboard)
        return int(sent.message_id)
    except Exception:
        return None


def _escaped_focus_line(focus: dict[str, Any] | None, tz) -> str | None:
    if _original_focus_line is None:
        return None
    if not focus:
        return _original_focus_line(focus, tz)
    safe_focus = dict(focus)
    safe_focus["title"] = h(str(focus.get("title") or ""))
    safe_focus["code"] = h(str(focus.get("code") or "INBOX"))
    return _original_focus_line(safe_focus, tz)


def install_product_mode_overrides() -> None:
    global _installed, _original_preview, _original_focus_line, _original_rerender
    if _installed:
        return

    import bot.services.freeform_intake as freeform_intake

    _original_preview = freeform_intake.create_pending_preview
    _original_focus_line = product_mode._focus_line
    _original_rerender = freeform_intake._rerender_with_toast

    freeform_intake._rerender_with_toast = _safe_rerender_with_toast
    freeform_intake.create_pending_preview = _preview_without_stale_draft_fingerprint
    freeform_intake._send_batch_summary = _send_accurate_batch_summary
    product_mode._focus_line = _escaped_focus_line

    _installed = True
