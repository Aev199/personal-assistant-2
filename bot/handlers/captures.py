"""Read capture receipts and open individual drafts inside the SPA."""
from aiogram import F

from bot.db.runtime_state import get_pending_action
from bot.services.capture_receipts import load_receipt, render_receipt, render_receipt_list
from bot.services.pending_actions import _preview_keyboard, _preview_text
from bot.ui.render import ui_render


async def cb_capture(callback, state, db_pool, deps):
    await callback.answer()
    await state.clear()
    parts = (callback.data or "").split(":")
    try:
        if parts[1] == "list":
            return await render_receipt_list(callback.message, db_pool, page=int(parts[2]))
        action, capture_id, index = parts[1], parts[2], int(parts[3])
    except (ValueError, IndexError):
        return
    if action == "open":
        return await render_receipt(callback.message, db_pool, capture_id, page=index)
    if action == "source":
        return await render_receipt(callback.message, db_pool, capture_id, source_page=index)
    if action != "draft":
        return
    receipt = await load_receipt(db_pool, int(callback.message.chat.id), capture_id)
    if not receipt or index not in {item.get("pending_action_id") for item in receipt.get("items", [])}:
        return
    async with db_pool.acquire() as conn:
        pending = await get_pending_action(conn, chat_id=int(callback.message.chat.id), pending_action_id=index)
    if not pending or pending["status"] != "pending":
        return await render_receipt(callback.message, db_pool, capture_id)
    return await ui_render(
        bot=callback.message.bot, db_pool=db_pool, chat_id=int(callback.message.chat.id),
        text=_preview_text(pending["kind"], pending["payload"], tz_name=deps.tz_name),
        reply_markup=_preview_keyboard(pending["kind"], index, pending["payload"]),
        screen="llm_draft", payload={"capture_id": capture_id, "pending_action_id": index},
        fallback_message=callback.message, parse_mode=None,
    )


def register(dp):
    dp.callback_query.register(cb_capture, F.data.startswith("capture:"))
