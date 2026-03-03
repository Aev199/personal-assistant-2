"""Global error handler for aiogram.

This is the production safety net: any unhandled exception in handlers ends up
here.

We intentionally avoid heavy logic in the handler itself and delegate to
``bot.services.error_handler.ErrorHandler`` which:
- logs in a structured way
- optionally notifies user/admin (rate-limited)
"""

from __future__ import annotations

from typing import Any, Optional

from aiogram import Bot, Dispatcher
from aiogram.types import ErrorEvent

from bot.deps import AppDeps
from bot.services.error_handler import ErrorContext, create_error_handler
from bot.services.logger import get_logger


def _extract_update_context(event: ErrorEvent) -> tuple[int, int, Optional[str], Optional[str]]:
    """Best-effort extraction of user/chat + message/callback payload."""

    user_id = 0
    chat_id = 0
    message_text: Optional[str] = None
    callback_data: Optional[str] = None

    upd = getattr(event, "update", None)
    if upd is None:
        return user_id, chat_id, message_text, callback_data

    msg = getattr(upd, "message", None)
    if msg is not None:
        try:
            user_id = int(getattr(getattr(msg, "from_user", None), "id", 0) or 0)
            chat_id = int(getattr(getattr(msg, "chat", None), "id", 0) or 0)
            message_text = getattr(msg, "text", None)
        except Exception:
            pass
        return user_id, chat_id, message_text, callback_data

    cb = getattr(upd, "callback_query", None)
    if cb is not None:
        try:
            user_id = int(getattr(getattr(cb, "from_user", None), "id", 0) or 0)
            msg2 = getattr(cb, "message", None)
            chat_id = int(getattr(getattr(msg2, "chat", None), "id", 0) or 0)
            callback_data = getattr(cb, "data", None)
            # Sometimes helpful:
            if msg2 is not None:
                message_text = getattr(msg2, "text", None)
        except Exception:
            pass

    return user_id, chat_id, message_text, callback_data


def register(dp: Dispatcher) -> None:
    log = get_logger("bot.handlers.errors")

    async def on_error(event: ErrorEvent, bot: Bot, deps: AppDeps, **data: Any) -> Any:
        exc = getattr(event, "exception", None)
        if exc is None:
            return

        user_id, chat_id, message_text, callback_data = _extract_update_context(event)

        # aiogram doesn't reliably provide handler name here; keep it simple.
        handler_name = "unhandled"
        if callback_data:
            handler_name = "callback"
        elif message_text is not None:
            handler_name = "message"

        # Ensure deps has an ErrorHandler instance
        eh = getattr(deps, "error_handler", None)
        if eh is None:
            eh = create_error_handler(
                bot=bot,
                admin_id=int(deps.admin_id or 0),
                logger=get_logger("bot.error_handler"),
                max_notifications_per_minute=5,
            )
            deps.error_handler = eh

        # Always log the raw exception once (even if notifications are disabled)
        log.error(
            "unhandled exception",
            error=exc,
            update_type=handler_name,
            user_id=user_id,
            chat_id=chat_id,
        )

        # Notify only if we have a chat_id/user_id.
        notify_user = bool(getattr(deps, "error_notify_user", True)) and (chat_id != 0)
        notify_admin = bool(getattr(deps, "error_notify_admin", True)) and (int(deps.admin_id or 0) > 0)

        ctx = ErrorContext(
            user_id=int(user_id or 0),
            chat_id=int(chat_id or 0),
            handler_name=handler_name,
            message_text=message_text,
            callback_data=callback_data,
        )

        try:
            await eh.handle_error(exc, ctx, notify_user=notify_user, notify_admin=notify_admin)
        except Exception as e2:
            # Never let the error handler crash the bot.
            log.error("error handler failed", error=e2)

    # Register in a way compatible with aiogram 3
    try:
        dp.errors.register(on_error)
    except Exception:
        try:
            dp.error.register(on_error)  # type: ignore[attr-defined]
        except Exception:
            # As a last resort, do nothing (bot still runs, but without global handler)
            log.warning("failed to register global error handler")
