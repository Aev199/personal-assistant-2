"""Cookie authentication for the same-origin web client; native Bearer stays intact."""
from collections import deque
from datetime import datetime, timezone
import os
import secrets
import time
from urllib.parse import urlsplit

import asyncpg
from aiohttp import web
from bot.services import web_sessions

COOKIE = 'assistant_session'
IDENTITY = web.RequestKey('assistant_browser_identity', dict) if hasattr(web, 'RequestKey') else 'assistant_browser_identity'


def configured_token() -> str:
    return (os.getenv('ASSISTANT_API_TOKEN') or os.getenv('COMPANION_API_TOKEN') or '').strip()


def public_origin() -> str:
    raw = (os.getenv('ASSISTANT_PUBLIC_ORIGIN') or '').strip()
    parsed = urlsplit(raw)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
        return ''
    return raw.rstrip('/')


def browser_identity(request: web.Request) -> dict | None:
    return request.get(IDENTITY) if isinstance(request, web.Request) else None


def error(code: str, status: int) -> web.Response:
    return web.json_response({'ok': False, 'error': code}, status=status, headers={'Cache-Control':'no-store'})


def attach_auth(app: web.Application, ctx) -> None:
    # ponytail: single-process per-IP throttle, bounded by a 30-failure global
    # window; move to shared storage before deploying multiple web workers.
    failures = deque(maxlen=30)

    @web.middleware
    async def auth(request, handler):
        is_api = request.path.startswith('/api/v1/')
        if is_api and not request.headers.get('Authorization'):
            pool = ctx.deps.db_pool
            secret = request.cookies.get(COOKIE, '')
            if pool and secret:
                try:
                    identity = await web_sessions.load_session(pool, secret, configured_token())
                except (asyncpg.PostgresError, OSError, TimeoutError):
                    return error('db_unavailable', 503)
                if identity and identity['expires_at'] > datetime.now(timezone.utc):
                    request[IDENTITY] = identity
                    if request.method not in ('GET','HEAD','OPTIONS') and not (request.path == '/api/v1/session' and request.method == 'POST'):
                        csrf = request.headers.get('X-CSRF-Token','')
                        if not public_origin() or request.headers.get('Origin') != public_origin() or not secrets.compare_digest(csrf.encode(), identity['csrf_token'].encode()):
                            return error('csrf_failed', 403)
        try:
            response = await handler(request)
        except web.HTTPException as exc:
            if is_api:
                exc.headers['Cache-Control'] = 'no-store'
            raise
        if is_api:
            response.headers['Cache-Control'] = 'no-store'
        return response

    async def session(request):
        if request.method == 'POST':
            origin = public_origin()
            if not origin or not configured_token():
                return error('web_not_configured', 503)
            if request.headers.get('Origin') != origin:
                return error('origin_not_allowed', 403)
            now = time.monotonic()
            while failures and failures[0][0] <= now - 60:
                failures.popleft()
            remote = request.remote or 'unknown'
            if len(failures) >= 30 or sum(ip == remote for _, ip in failures) >= 5:
                response = error('login_rate_limited', 429)
                response.headers['Retry-After'] = '60'
                return response
            try:
                data = await request.json()
            except (ValueError, web.HTTPException):
                return error('invalid_json', 400)
            supplied = data.get('token') if isinstance(data, dict) else None
            if not isinstance(supplied, str) or not secrets.compare_digest(supplied.encode(), configured_token().encode()):
                failures.append((now, remote))
                return error('unauthorized', 401)
            if not ctx.deps.db_pool:
                return error('db_unavailable', 503)
            try:
                secret, row = await web_sessions.create_session(ctx.deps.db_pool, configured_token())
            except (asyncpg.PostgresError, OSError, TimeoutError):
                return error('db_unavailable', 503)
            response = web.json_response({'ok': True, 'csrf_token': row['csrf_token']}, status=201)
            response.set_cookie(COOKIE, secret, max_age=web_sessions.SESSION_SECONDS,
                                path='/', secure=True, httponly=True, samesite='Strict')
            return response
        identity = browser_identity(request)
        if not identity:
            return error('unauthorized', 401)
        if request.method == 'DELETE':
            await web_sessions.delete_session(ctx.deps.db_pool, request.cookies[COOKIE])
            response = web.json_response({'ok': True})
            response.del_cookie(COOKIE, path='/', secure=True, httponly=True, samesite='Strict')
            return response
        return web.json_response({'ok': True, 'csrf_token': identity['csrf_token']})

    app.middlewares.append(auth)
    app.router.add_post('/api/v1/session', session)
    app.router.add_get('/api/v1/session', session)
    app.router.add_delete('/api/v1/session', session)
