"""The whole server assembles in both Telegram modes without network access."""
import pytest

from bot.runtime import create_app

ENV = {
    "BOT_TOKEN": "123456:TEST_TOKEN_FOR_ASSEMBLY_ONLY",
    "ADMIN_ID": "42",
    "INTERNAL_API_KEY": "internal",
    "DATABASE_URL": "postgresql://unused/unused",
    "BOT_TIMEZONE": "Europe/Moscow",
}


def _paths(app):
    return {route.resource.canonical for route in app.router.routes()}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("WEBHOOK_URL", raising=False)
    monkeypatch.delenv("BOT_RUNTIME_MODE", raising=False)


def test_polling_mode_serves_api_web_and_tick():
    paths = _paths(create_app())
    for path in ("/tick", "/health", "/ping", "/api/v1/today", "/api/v1/intake", "/app/"):
        assert path in paths
    assert "/webhook" not in paths
    for gone in ("/backup", "/keepalive"):
        assert gone not in paths


def test_webhook_mode_adds_the_telegram_endpoint(monkeypatch):
    monkeypatch.setenv("WEBHOOK_URL", "https://example.invalid")
    assert "/webhook" in _paths(create_app())
