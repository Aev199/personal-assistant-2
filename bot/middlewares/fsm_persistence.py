"""Middleware for persisting FSM state to the database."""

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, Update
from aiogram.fsm.context import FSMContext

class FsmPersistenceMiddleware(BaseMiddleware):
    """Saves FSM state to `conversation_state` after handler execution."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        # 1. Execute the handler
        result = await handler(event, data)
        
        # 2. Extract needed objects
        fsm_context: Optional[FSMContext] = data.get("state")
        db_pool = data.get("db_pool")
        
        # We need a chat_id to save the state
        chat_id = None
        if isinstance(event, Message):
            chat_id = int(event.chat.id)
        elif isinstance(event, CallbackQuery) and event.message:
            chat_id = int(event.message.chat.id)
            
        if not fsm_context or not db_pool or not chat_id:
            return result

        # 3. Read current FSM state and data
        current_state = await fsm_context.get_state()
        state_data = await fsm_context.get_data()
        
        # Only persist if we are in an active state
        if not current_state:
            # User cleared state (e.g. state.clear())
            async with db_pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM conversation_state WHERE chat_id=$1 AND flow='fsm'",
                    chat_id
                )
            return result

        # 4. Save to DB
        expires_at = datetime.now(timezone.utc) + timedelta(days=1)
        payload_json = json.dumps(state_data, ensure_ascii=False)
        
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO conversation_state (chat_id, flow, step, payload_json, expires_at)
                VALUES ($1, 'fsm', $2, $3, $4)
                ON CONFLICT (chat_id, flow)
                DO UPDATE SET 
                    step = EXCLUDED.step,
                    payload_json = EXCLUDED.payload_json,
                    expires_at = EXCLUDED.expires_at,
                    updated_at = NOW()
                """,
                chat_id,
                str(current_state),
                payload_json,
                expires_at,
            )

        return result

async def recover_fsm_state(chat_id: int, db_pool: Any, state: FSMContext) -> bool:
    """Attempt to recover FSM state from DB for this user."""
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT step, payload_json FROM conversation_state WHERE chat_id=$1 AND flow='fsm' AND expires_at > NOW()",
            chat_id
        )
        if not row:
            return False
            
        step = row["step"]
        payload_json = row["payload_json"]
        
        try:
            data = json.loads(payload_json) if payload_json else {}
        except Exception:
            data = {}
            
        await state.set_state(step)
        await state.update_data(**data)
        return True
