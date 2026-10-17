"""Telegram callbacks for «На сегодня»."""
from aiogram import F

from bot.services.day_plan import DayPlanError, local_today, set_planned
from bot.ui.day_plan import GREETING, ui_render_day_plan


async def cb_day_plan(callback, db_pool, deps):
    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    notice = None
    if action in {"add", "del"}:
        try:
            task_id = int(parts[2])
        except (IndexError, ValueError):
            await callback.answer()
            return
        try:
            async with db_pool.acquire() as conn:
                async with conn.transaction():
                    await set_planned(
                        conn,
                        task_id=task_id,
                        planned=action == "add",
                        today=local_today(deps.tz_name),
                    )
            notice = "Добавлено на сегодня" if action == "add" else "Убрано из плана"
        except DayPlanError:
            notice = "Эта задача уже закрыта"
    elif action != "pick":
        await callback.answer()
        return

    await callback.answer(notice or "")
    message = callback.message
    greeting = bool(message and (message.html_text or message.text or "").startswith(GREETING.split(" ")[0]))
    await ui_render_day_plan(
        bot=message.bot,
        db_pool=db_pool,
        chat_id=int(message.chat.id),
        tz_name=deps.tz_name,
        greeting=greeting,
        fallback_message=message,
        preferred_message_id=int(message.message_id),
    )


def register(dp):
    dp.callback_query.register(cb_day_plan, F.data.startswith("plan:"))
