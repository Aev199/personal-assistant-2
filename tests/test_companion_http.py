import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

from aiohttp import web

from bot.http import companion


def test_api_has_one_canonical_route_set_without_focus_or_legacy_aliases():
    app = web.Application()
    companion.attach_companion_routes(app, SimpleNamespace())
    routes = {(route.method, route.resource.canonical) for route in app.router.routes()}

    for route in [
        ("GET", "/api/v1/today"), ("GET", "/api/v1/tasks"), ("GET", "/api/v1/projects"), ("GET", "/api/v1/ideas"),
        ("POST", "/api/v1/ideas/{idea_id}/promote"), ("POST", "/api/v1/ideas/{idea_id}/archive"),
        ("PATCH", "/api/v1/tasks/{task_id}"), ("POST", "/api/v1/intake"), ("POST", "/api/v1/intake/audio"),
        ("GET", "/api/v1/intake/pending"), ("POST", "/api/v1/intake/{pending_action_id}/confirm"),
        ("POST", "/api/v1/intake/{pending_action_id}/cancel"), ("POST", "/api/v1/tasks/{task_id}/done"),
        ("POST", "/api/v1/tasks/{task_id}/plan"), ("POST", "/api/v1/tasks/{task_id}/unplan"),
        ("POST", "/api/v1/attention/dismiss-event"), ("POST", "/api/v1/reminders/{reminder_id}/snooze"),
        ("POST", "/api/v1/reminders/{reminder_id}/ack"),
    ]:
        assert route in routes
    paths = {path for _method, path in routes}
    assert not any(path.startswith("/api/v1/companion/") for path in paths)
    for gone in ("/api/v1/capture", "/api/v1/tasks/{task_id}/focus", "/api/v1/tasks/{task_id}/unfocus", "/api/v1/tasks/{task_id}/steps"):
        assert gone not in paths


def test_api_needs_the_app_token(monkeypatch):
    monkeypatch.setenv("ASSISTANT_API_TOKEN", "assistant-secret")
    good = SimpleNamespace(headers={"Authorization": "Bearer assistant-secret"}, cookies={})
    bad = SimpleNamespace(headers={"Authorization": "Bearer wrong"}, cookies={})
    assert companion._authorized(good) is True
    assert companion._authorized(bad) is False

    monkeypatch.delenv("ASSISTANT_API_TOKEN")
    monkeypatch.setenv("COMPANION_API_TOKEN", "older-name")
    assert companion._authorized(SimpleNamespace(headers={"Authorization": "Bearer older-name"}, cookies={})) is True
    monkeypatch.delenv("COMPANION_API_TOKEN")
    assert companion._authorized(good) is False














def test_utc_aware_normalizes_naive_and_aware_values():
    naive = datetime(2026, 9, 12, 10, 30)
    aware = datetime(2026, 9, 12, 12, 30, tzinfo=timezone.utc)

    normalized_naive = companion._utc_aware(naive)
    normalized_aware = companion._utc_aware(aware)

    assert normalized_naive == datetime(2026, 9, 12, 10, 30, tzinfo=timezone.utc)
    assert normalized_aware == aware
    assert companion._utc_aware(None) is None








def test_calendar_attention_dismissals_are_serialized_across_clients():
    source = open(companion.__file__, encoding="utf-8").read()

    assert "async def _lock_attention_state(" in source

    start = source.index("async def handle_event_dismiss")
    end = source.index("async def handle_tasks", start)
    block = source[start:end]

    assert "async with conn.transaction():" in block
    assert "_lock_attention_state(" in block
    assert "ATTENTION_DISMISSED_EVENTS_FLOW" in block
    assert block.index("_lock_attention_state(") < block.index("get_conversation_state(")
    assert block.index("get_conversation_state(") < block.index("set_conversation_state(")










def test_attention_selector_keeps_unscheduled_work_visible():
    now = datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc)
    end = datetime(2026, 9, 21, 21, 0, tzinfo=timezone.utc)
    rows = [
        {"id": 1, "status": "todo", "deadline": datetime(2026, 9, 18, 9, 0), "created_at": datetime(2026, 9, 1)},
        {"id": 2, "status": "todo", "deadline": datetime(2026, 9, 19, 9, 0), "created_at": datetime(2026, 9, 2)},
        {"id": 3, "status": "todo", "deadline": datetime(2026, 9, 20, 9, 0), "created_at": datetime(2026, 9, 3)},
        {"id": 4, "status": "todo", "deadline": datetime(2026, 9, 20, 12, 0), "created_at": datetime(2026, 9, 4)},
        {"id": 5, "status": "todo", "deadline": None, "created_at": datetime(2026, 9, 5)},
        {"id": 6, "status": "todo", "deadline": datetime(2026, 9, 22, 12, 0), "created_at": datetime(2026, 9, 6)},
    ]

    selected = companion._select_attention_tasks(rows, now_utc=now, end_utc_aware=end)

    assert len(selected) == 5
    assert 5 in {row["id"] for row in selected}
    urgent = [
        row for row in selected
        if companion._utc_aware(row["deadline"]) is not None
        and companion._utc_aware(row["deadline"]) < end
    ]
    assert len(urgent) == 3


def test_attention_selector_prefers_in_progress_then_due_today():
    now = datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc)
    end = datetime(2026, 9, 22, 0, 0, tzinfo=timezone.utc)
    rows = [
        {"id": 10, "status": "todo", "deadline": datetime(2026, 9, 21, 11, 0), "created_at": datetime(2026, 9, 1)},
        {"id": 11, "status": "in_progress", "deadline": None, "created_at": datetime(2026, 9, 20)},
        {"id": 12, "status": "todo", "deadline": datetime(2026, 9, 20, 15, 0), "created_at": datetime(2026, 9, 2)},
    ]

    selected = companion._select_attention_tasks(rows, now_utc=now, end_utc_aware=end)

    assert [row["id"] for row in selected] == [11, 10, 12]






def test_native_snooze_preserves_repeating_series_and_rejects_stale_future_tap():
    now = datetime(2026, 9, 24, 9, 0, tzinfo=timezone.utc)

    due_repeat = {
        "status": "pending",
        "repeat": "daily",
        "telegram_message_id": None,
        "remind_at": datetime(2026, 9, 24, 8, 59),
    }
    advanced_repeat = {
        "status": "pending",
        "repeat": "daily",
        "telegram_message_id": None,
        "remind_at": datetime(2026, 9, 25, 9, 0),
    }
    delivered_repeat = {
        **advanced_repeat,
        "telegram_message_id": 321,
    }
    sent_single = {
        "status": "sent",
        "repeat": "none",
        "telegram_message_id": None,
        "remind_at": datetime(2026, 9, 24, 8, 59),
    }

    assert companion._native_reminder_snooze_mode(due_repeat, now_utc=now) == "advance_repeat"
    assert companion._native_reminder_snooze_mode(advanced_repeat, now_utc=now) == "already_handled"
    assert companion._native_reminder_snooze_mode(delivered_repeat, now_utc=now) == "retry_delivered_repeat"
    assert companion._native_reminder_snooze_mode(sent_single, now_utc=now) == "already_handled"

    source = open(companion.__file__, encoding="utf-8").read()
    start = source.index("async def handle_reminder_snooze")
    end = source.index("async def handle_event_dismiss", start)
    block = source[start:end]

    assert "next_repeat_time_utc_naive" in block
    assert "retry_delivered_repeat" in block
    assert "INSERT INTO reminders" in block
    assert "'none', 'pending'" in block


def test_native_reminder_ack_preserves_repeat_and_ignores_stale_future_occurrence():
    source = open(companion.__file__, encoding="utf-8").read()
    start = source.index("async def handle_reminder_ack")
    end = source.index("async def handle_reminder_snooze", start)
    block = source[start:end]

    assert "next_repeat_time_utc_naive" in block
    assert "already_advanced" in block
    assert '"already_handled"' in block
    assert "status='pending'" in block
    assert "status='sent'" in block
    assert "claim_token=NULL" in block


def test_today_keeps_claimed_due_reminder_visible_while_telegram_delivery_is_in_flight():
    source = open(companion.__file__, encoding="utf-8").read()
    start = source.index("reminder_rows = await conn.fetch")
    end = source.index("selected_task_rows =", start)
    reminder_query = source[start:end]

    assert "('pending', 'retry', 'claimed')" in reminder_query
    assert "COALESCE(is_sent, FALSE)=FALSE" in reminder_query


def test_today_keeps_missed_reminders_visible_without_letting_the_oldest_win():
    source = open(companion.__file__, encoding="utf-8").read()
    start = source.index("reminder_rows = await conn.fetch")
    end = source.index("selected_task_rows =", start)
    reminder_query = source[start:end]

    assert "AND remind_at >= $2" not in reminder_query
    assert "AND remind_at < $3" in reminder_query
    assert "CASE WHEN remind_at <= $2 THEN 0 ELSE 1 END" in reminder_query
    assert "CASE WHEN remind_at <= $2 THEN remind_at END DESC" in reminder_query


def test_calendar_snapshot_budget_does_not_block_today():
    async def scenario():
        expected = companion.TodayCalendarSnapshot(events=(), unavailable=False)

        async def slow_calendar():
            await asyncio.sleep(0.02)
            return expected

        task = asyncio.create_task(slow_calendar())
        immediate = await companion._calendar_snapshot_with_budget(task, timeout_sec=0)

        assert immediate.events == ()
        assert immediate.unavailable is False
        assert immediate.pending is True
        assert task.cancelled() is False
        resolved = await task
        assert resolved == expected
        assert resolved.pending is False

    asyncio.run(scenario())


def test_dismissed_event_items_drop_expired_values():
    now = datetime(2026, 9, 22, 9, 0, tzinfo=timezone.utc)
    state = {
        "payload": {
            "items": [
                {"id": "keep", "until": "2026-09-22T10:00:00+00:00"},
                {"id": "expired", "until": "2026-09-22T08:59:00+00:00"},
                {"id": "", "until": "2026-09-22T10:00:00+00:00"},
                {"id": "bad", "until": "not-a-date"},
            ]
        }
    }

    items = companion._active_dismissed_event_items(state, now_utc=now)

    assert items == [{"id": "keep", "until": "2026-09-22T10:00:00+00:00"}]


def test_native_backlog_is_complete_and_carries_edit_version(monkeypatch):
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    monkeypatch.setenv("ASSISTANT_API_TOKEN", "app")
    version = datetime(2026, 10, 17, 9, 30, 0, 123456, tzinfo=timezone.utc)
    rows = [
        {
            "id": index,
            "title": f"Задача {index}",
            "deadline": None,
            "status": "todo",
            "kind": "task",
            "created_at": version,
            "updated_at": version,
            "project_code": "INBOX",
            "assignee": "",
        }
        for index in range(1, 301)
    ]
    captured = {}

    class Conn:
        async def fetch(self, query, *args):
            captured["args"] = args
            return rows

    class Pool:
        def acquire(self):
            conn = Conn()

            class Acquire:
                async def __aenter__(self):
                    return conn

                async def __aexit__(self, *_exc):
                    return False

            return Acquire()

    class Request:
        headers = {"Authorization": "Bearer app"}
        query = {"limit": "500"}
        cookies = {}

    ctx = SimpleNamespace(deps=SimpleNamespace(db_pool=Pool(), tz_name="Europe/Moscow", admin_id=1))
    with patch.object(companion, "get_conversation_state", AsyncMock(return_value=None)):
        response = asyncio.run(companion.handle_tasks(Request(), ctx))

    body = json.loads(response.body)
    assert response.status == 200
    assert len(body["tasks"]) == 300
    assert captured["args"] == (companion.TASK_BACKLOG_LIMIT,)
    assert body["tasks"][0]["updated_at"] == version.isoformat()
