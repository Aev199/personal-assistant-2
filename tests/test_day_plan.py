import asyncio
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from bot.http import companion
from bot.services.day_plan import in_today_plan, local_today
from bot.services.product_mode_spa import _habit_policy
from bot.ui.day_plan import plan_picker_view

MSK = ZoneInfo("Europe/Moscow")
TODAY = date(2026, 10, 19)


def test_explicit_plan_and_due_today_are_on_the_list():
    assert in_today_plan({"planned_for": TODAY}, today=TODAY, tz=MSK)
    # 10:00 UTC is 13:00 in Moscow on the same day.
    due = {"deadline": datetime(2026, 10, 19, 10, 0)}
    assert in_today_plan(due, today=TODAY, tz=MSK)
    # 22:30 UTC on the 18th is already the 19th in Moscow.
    assert in_today_plan({"deadline": datetime(2026, 10, 18, 22, 30)}, today=TODAY, tz=MSK)


def test_removed_due_task_and_yesterdays_plan_are_not_on_the_list():
    removed = {"deadline": datetime(2026, 10, 19, 10, 0), "plan_dismissed_for": TODAY}
    assert not in_today_plan(removed, today=TODAY, tz=MSK)
    assert not in_today_plan({"planned_for": date(2026, 10, 18)}, today=TODAY, tz=MSK)
    assert not in_today_plan({"deadline": datetime(2026, 10, 20, 10, 0)}, today=TODAY, tz=MSK)
    assert not in_today_plan({}, today=TODAY, tz=MSK)


def test_local_today_follows_the_user_timezone():
    late_utc = datetime(2026, 10, 18, 22, 30, tzinfo=ZoneInfo("UTC"))
    assert local_today("Europe/Moscow", now=late_utc) == TODAY


def test_morning_picker_is_one_tap_per_task():
    plan = [{"id": 1, "title": "Ответить подрядчику", "deadline": datetime(2026, 10, 19, 11, 0)}]
    candidates = [
        {"id": 2, "title": "Проверить смету", "deadline": datetime(2026, 10, 15, 9, 0)},
        {"id": 3, "title": "Позвонить в банк", "deadline": None},
    ]
    text, markup = plan_picker_view(plan, candidates, tz=MSK, today=TODAY, greeting=True)
    assert text.startswith("☀️ <b>Что сегодня?</b>")
    assert "1. Ответить подрядчику · до 14:00" in text
    data = [row[0].callback_data for row in markup.inline_keyboard]
    assert data == ["plan:del:1", "plan:add:2", "plan:add:3", "nav:today"]
    assert "просрочено" in markup.inline_keyboard[1][0].text


def test_full_plan_stops_offering_more():
    plan = [{"id": i, "title": f"Дело {i}", "deadline": None} for i in range(1, 6)]
    text, markup = plan_picker_view(plan, [{"id": 9, "title": "Ещё", "deadline": None}], tz=MSK, today=TODAY)
    assert "Нажмите, чтобы добавить" not in text
    assert all(not row[0].callback_data.startswith("plan:add") for row in markup.inline_keyboard)


def test_morning_question_needs_only_some_active_task_and_never_weekends():
    weekday = datetime(2026, 10, 19, 10, 5, tzinfo=MSK)
    saturday = datetime(2026, 10, 17, 10, 5, tzinfo=MSK)
    backlog_only = {"today": 0, "overdue": 0, "reminders": 0, "active": 4, "inbox": 0}
    allowed = _habit_policy(now_local=weekday, snapshot=backlog_only, onboarding_completed=True, active_internal=4)
    assert allowed["morning_allowed"]
    weekend = _habit_policy(now_local=saturday, snapshot=backlog_only, onboarding_completed=True, active_internal=4)
    assert not weekend["morning_allowed"]
    empty = _habit_policy(now_local=weekday, snapshot={"active": 0}, onboarding_completed=True, active_internal=0)
    assert not empty["morning_allowed"]


def test_default_times_are_ten_and_five(monkeypatch):
    from bot.services import product_mode
    monkeypatch.delenv("ASSISTANT_MORNING_BRIEF_HOUR", raising=False)
    monkeypatch.delenv("ASSISTANT_EVENING_NUDGE_HOUR", raising=False)
    import inspect
    from bot.services import product_mode_spa
    source = inspect.getsource(product_mode_spa.maybe_send_habit_messages_spa)
    assert '"ASSISTANT_MORNING_BRIEF_HOUR", 10' in source
    assert '"ASSISTANT_EVENING_NUDGE_HOUR", 17' in source
    assert product_mode._env_hour("ASSISTANT_MORNING_BRIEF_HOUR", 10) == 10


def test_plan_routes_exist_and_require_a_token(monkeypatch):
    app = web_app()
    paths = {route.resource.canonical for route in app.router.routes()}
    assert "/api/v1/tasks/{task_id}/plan" in paths
    assert "/api/v1/tasks/{task_id}/unplan" in paths

    monkeypatch.setenv("ASSISTANT_API_TOKEN", "app")

    class Request:
        headers = {"Authorization": "Bearer wrong"}
        cookies = {}
        match_info = {"task_id": "1"}

    ctx = SimpleNamespace(deps=SimpleNamespace(db_pool=object(), tz_name="Europe/Moscow"))
    response = asyncio.run(companion.handle_task_plan(Request(), ctx, planned=True))
    assert response.status == 401


def web_app():
    from aiohttp import web
    app = web.Application()
    companion.attach_companion_routes(app, SimpleNamespace(deps=SimpleNamespace()))
    return app


def test_telegram_home_is_today_not_the_whole_backlog():
    from unittest.mock import AsyncMock, patch
    from bot.ui import simple_daily

    message = SimpleNamespace(chat=SimpleNamespace(id=1), bot=SimpleNamespace())
    with patch.object(simple_daily, "ui_render_today", AsyncMock(return_value=5)) as today:
        asyncio.run(simple_daily.ui_render_home(message, object(), tz_name="Europe/Moscow"))
    today.assert_awaited_once()
    assert today.await_args.kwargs["icloud"] is simple_daily._default_icloud
