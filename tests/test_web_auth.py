"""Real HTTP auth checks; only the external session store is substituted."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from bot.http import companion


def test_browser_session_lifecycle_and_csrf(monkeypatch):
    async def run():
        from bot.http.web_app import attach_web_routes
        from bot.services import web_sessions
        monkeypatch.setenv('ASSISTANT_API_TOKEN', 'app-secret')
        monkeypatch.setenv('COMPANION_WIDGET_TOKEN', 'widget-secret')
        monkeypatch.setenv('ASSISTANT_PUBLIC_ORIGIN', 'https://assistant.example')
        sessions = {}
        async def create(pool, token):
            sessions['cookie-secret'] = {'csrf_token': 'csrf', 'expires_at': datetime.now(timezone.utc)+timedelta(days=1)}
            return 'cookie-secret', sessions['cookie-secret']
        async def load(pool, secret, token):
            return sessions.get(secret) if token == 'app-secret' else None
        async def delete(pool, secret):
            sessions.pop(secret, None)
        monkeypatch.setattr(web_sessions, 'create_session', create)
        monkeypatch.setattr(web_sessions, 'load_session', load)
        monkeypatch.setattr(web_sessions, 'delete_session', delete)
        ctx = SimpleNamespace(deps=SimpleNamespace(db_pool=object()))
        app = web.Application()
        attach_web_routes(app, ctx)
        async def protected(request):
            return web.json_response({'ok': companion._authorized(request)})
        app.router.add_get('/api/v1/probe', protected)
        app.router.add_post('/api/v1/probe', protected)
        async with TestClient(TestServer(app)) as c:
            origin = {'Origin':'https://assistant.example'}
            assert (await c.get('/app/')).status == 200
            assert (await (await c.get('/api/v1/probe')).json())['ok'] is False
            assert (await c.post('/api/v1/session', json={'token':'widget-secret'}, headers=origin)).status == 401
            assert (await c.post('/api/v1/session', json={'token':'app-secret'}, headers={'Origin':'https://evil.example'})).status == 403
            r = await c.post('/api/v1/session', json={'token':'app-secret'}, headers=origin)
            assert r.status == 201
            cookie = r.cookies['assistant_session']
            assert cookie['httponly'] and cookie['secure'] and cookie['samesite'] == 'Strict'
            headers = {'Cookie':'assistant_session=cookie-secret'}
            assert (await (await c.get('/api/v1/probe', headers=headers)).json())['ok'] is True
            assert (await c.post('/api/v1/probe', headers=headers)).status == 403
            good = {**headers, **origin, 'X-CSRF-Token':'csrf'}
            assert (await c.post('/api/v1/probe', headers=good)).status == 200
            assert (await c.post('/api/v1/probe', headers={**good,'Origin':'https://evil.example'})).status == 403
            a = await (await c.get('/api/v1/session', headers=headers)).json()
            b = await (await c.get('/api/v1/session', headers=headers)).json()
            assert a['csrf_token'] == b['csrf_token'] == 'csrf'
            monkeypatch.setenv('ASSISTANT_API_TOKEN', 'rotated')
            assert (await c.get('/api/v1/session', headers=headers)).status == 401
            monkeypatch.setenv('ASSISTANT_API_TOKEN', 'app-secret')
            assert (await c.delete('/api/v1/session', headers=good)).status == 200
            assert (await c.get('/api/v1/session', headers=headers)).status == 401
    asyncio.run(run())


def test_login_rate_limit_and_input_validation(monkeypatch):
    async def run():
        from bot.http.web_app import attach_web_routes
        monkeypatch.setenv('ASSISTANT_API_TOKEN','secret')
        monkeypatch.setenv('ASSISTANT_PUBLIC_ORIGIN','https://assistant.example')
        app=web.Application()
        attach_web_routes(app, SimpleNamespace(deps=SimpleNamespace(db_pool=object())))
        async with TestClient(TestServer(app)) as c:
            h={'Origin':'https://assistant.example'}
            for _ in range(5):
                assert (await c.post('/api/v1/session', json={'token':'bad'}, headers=h)).status == 401
            assert (await c.post('/api/v1/session', json={'token':'secret'}, headers=h)).status == 429
    asyncio.run(run())


def test_expired_session_is_not_authorized(monkeypatch):
    async def run():
        from bot.http.web_app import attach_web_routes
        from bot.services import web_sessions
        monkeypatch.setenv('ASSISTANT_API_TOKEN','secret')
        monkeypatch.setenv('ASSISTANT_PUBLIC_ORIGIN','https://assistant.example')
        monkeypatch.setattr(web_sessions,'load_session',AsyncMock(return_value={'csrf_token':'x','expires_at':datetime.now(timezone.utc)-timedelta(seconds=1)}))
        app=web.Application()
        attach_web_routes(app,SimpleNamespace(deps=SimpleNamespace(db_pool=object())))
        async with TestClient(TestServer(app)) as c:
            r=await c.get('/api/v1/session',headers={'Cookie':'assistant_session=expired'})
            assert r.status == 401
            assert r.headers['Cache-Control'] == 'no-store'
    asyncio.run(run())


def test_non_ascii_token_is_rejected_and_existing_session_can_reconnect(monkeypatch):
    async def run():
        from bot.http.web_app import attach_web_routes
        from bot.services import web_sessions
        monkeypatch.setenv('ASSISTANT_API_TOKEN','secret')
        monkeypatch.setenv('ASSISTANT_PUBLIC_ORIGIN','https://assistant.example')
        identity={'csrf_token':'csrf','expires_at':datetime.now(timezone.utc)+timedelta(days=1)}
        monkeypatch.setattr(web_sessions,'load_session',AsyncMock(return_value=identity))
        monkeypatch.setattr(web_sessions,'create_session',AsyncMock(return_value=('new-cookie',identity)))
        app=web.Application();attach_web_routes(app,SimpleNamespace(deps=SimpleNamespace(db_pool=object())))
        async with TestClient(TestServer(app)) as c:
            h={'Origin':'https://assistant.example'}
            assert (await c.post('/api/v1/session',json={'token':'не тот ключ'},headers=h)).status == 401
            # Startup network failure can show the connection form with a valid cookie.
            assert (await c.post('/api/v1/session',json={'token':'secret'},headers={**h,'Cookie':'assistant_session=old-cookie'})).status == 201
    asyncio.run(run())
