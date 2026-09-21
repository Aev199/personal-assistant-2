from datetime import datetime, timezone
from types import SimpleNamespace

from aiohttp import web

from bot.http import companion


def test_assistant_routes_include_canonical_and_legacy_aliases():
    app = web.Application()
    companion.attach_companion_routes(app, SimpleNamespace())

    routes = {(route.method, route.resource.canonical) for route in app.router.routes()}

    assert ("GET", "/api/v1/today") in routes
    assert ("GET", "/api/v1/tasks") in routes
    assert ("POST", "/api/v1/capture") in routes
    assert ("POST", "/api/v1/intake") in routes
    assert ("POST", "/api/v1/intake/{pending_action_id}/confirm") in routes
    assert ("POST", "/api/v1/intake/{pending_action_id}/cancel") in routes
    assert ("POST", "/api/v1/tasks/{task_id}/done") in routes

    assert ("GET", "/api/v1/companion/today") in routes
    assert ("POST", "/api/v1/companion/capture") in routes
    assert ("POST", "/api/v1/companion/tasks/{task_id}/done") in routes


def test_companion_auth_requires_dedicated_bearer_token(monkeypatch):
    monkeypatch.delenv("ASSISTANT_API_TOKEN", raising=False)
    monkeypatch.setenv("COMPANION_API_TOKEN", "pocket-secret")
    monkeypatch.setenv("COMPANION_WIDGET_TOKEN", "widget-secret")

    app_token = SimpleNamespace(headers={"Authorization": "Bearer pocket-secret"})
    widget_token = SimpleNamespace(headers={"Authorization": "Bearer widget-secret"})
    bad = SimpleNamespace(headers={"Authorization": "Bearer wrong"})
    legacy = SimpleNamespace(headers={"X-Internal-Key": "pocket-secret"})

    assert companion._authorized(app_token) is True
    assert companion._authorized(widget_token) is False
    assert companion._authorized(widget_token, allow_widget=True) is True
    assert companion._authorized(app_token, allow_widget=True) is True
    assert companion._authorized(bad, allow_widget=True) is False
    assert companion._authorized(legacy, allow_widget=True) is False


def test_assistant_api_token_is_supported(monkeypatch):
    monkeypatch.setenv("ASSISTANT_API_TOKEN", "assistant-secret")
    monkeypatch.delenv("COMPANION_API_TOKEN", raising=False)
    monkeypatch.delenv("COMPANION_WIDGET_TOKEN", raising=False)

    request = SimpleNamespace(headers={"Authorization": "Bearer assistant-secret"})
    assert companion._authorized(request) is True
    assert companion._authorized(request, allow_widget=True) is True


def test_companion_is_disabled_without_token(monkeypatch):
    monkeypatch.delenv("ASSISTANT_API_TOKEN", raising=False)
    monkeypatch.delenv("COMPANION_API_TOKEN", raising=False)
    monkeypatch.delenv("COMPANION_WIDGET_TOKEN", raising=False)
    request = SimpleNamespace(headers={"Authorization": "Bearer anything"})
    assert companion._authorized(request) is False
    assert companion._authorized(request, allow_widget=True) is False


def test_widget_token_does_not_grant_capture_scope(monkeypatch):
    monkeypatch.delenv("ASSISTANT_API_TOKEN", raising=False)
    monkeypatch.delenv("COMPANION_API_TOKEN", raising=False)
    monkeypatch.setenv("COMPANION_WIDGET_TOKEN", "widget-secret")
    request = SimpleNamespace(headers={"Authorization": "Bearer widget-secret"})

    assert companion._authorized(request) is False
    assert companion._authorized(request, allow_widget=True) is True


def test_utc_aware_normalizes_naive_and_aware_values():
    naive = datetime(2026, 9, 12, 10, 30)
    aware = datetime(2026, 9, 12, 12, 30, tzinfo=timezone.utc)

    normalized_naive = companion._utc_aware(naive)
    normalized_aware = companion._utc_aware(aware)

    assert normalized_naive == datetime(2026, 9, 12, 10, 30, tzinfo=timezone.utc)
    assert normalized_aware == aware
    assert companion._utc_aware(None) is None


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
