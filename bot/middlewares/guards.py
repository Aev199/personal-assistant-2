"""Aiogram middlewares for single-user isolation and update deduplication."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, Update

from bot.db.runtime_state import register_processed_update


class SingleUserGuardMiddleware(BaseMiddleware):
    def __init__(self, admin_id: int) -> None:
        self._admin_id = int(admin_id or 0)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if self._admin_id <= 0:
            return await handler(event, data)

        from_user = getattr(event, "from_user", None)
        if from_user is None and isinstance(event, Update):
            from_user = getattr(getattr(event, "message", None), "from_user", None)
            if from_user is None:
                from_user = getattr(getattr(event, "callback_query", None), "from_user", None)

        if from_user is None or int(getattr(from_user, "id", 0) or 0) != self._admin_id:
            if isinstance(event, CallbackQuery):
                try:
                    await event.answer("Недоступно", show_alert=True)
                except Exception:
                    pass
            elif isinstance(event, Message):
                try:
                    await event.answer("Недоступно")
                except Exception:
                    pass
            return None
        return await handler(event, data)


class ProcessedUpdateMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        update = data.get("event_update")
        if update is None and isinstance(event, Update):
            update = event
        update_id = getattr(update, "update_id", None)
        db_pool = data.get("db_pool")
        if not update_id or db_pool is None:
            return await handler(event, data)

        async with db_pool.acquire() as conn:
            first_seen = await register_processed_update(conn, int(update_id))
        if not first_seen:
            return None
        return await handler(event, data)
