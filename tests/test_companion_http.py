from datetime import datetime, timezone
from types import SimpleNamespace

from aiohttp import web

from bot.http import companion


def test_companion_routes_are_small_and_explicit():
    app = web.Application()
    companion.attach_companion_routes(app, SimpleNamespace())

    routes = {(route.method, route.resource.canonical) for route in app.router.routes()}
    assert ("GET", "/api/v1/companion/today") in routes
    assert ("POST", "/api/v1/companion/capture") in routes
    assert ("POST", "/api/v1/companion/tasks/{task_id}/done") in routes


def test_companion_auth_requires_dedicated_bearer_token(monkeypatch):
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


def test_companion_is_disabled_without_token(monkeypatch):
    monkeypatch.delenv("COMPANION_API_TOKEN", raising=False)
    monkeypatch.delenv("COMPANION_WIDGET_TOKEN", raising=False)
    request = SimpleNamespace(headers={"Authorization": "Bearer anything"})
    assert companion._authorized(request) is False
    assert companion._authorized(request, allow_widget=True) is False


def test_widget_token_does_not_grant_capture_scope(monkeypatch):
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
