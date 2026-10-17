"""Serve only the web application's public files, beside the existing API."""
from pathlib import Path
from aiohttp import web
from bot.http.web_auth import attach_auth

WEB_ROOT = Path(__file__).resolve().parent.parent / 'web'


def attach_web_routes(app: web.Application, ctx) -> None:
    attach_auth(app, ctx)
    async def asset(request):
        name = request.match_info.get('name', 'index.html')
        if name not in {'index.html','app.css','app.js'}:
            raise web.HTTPNotFound()
        path = WEB_ROOT / name
        if not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={
            'Cache-Control':'no-store', 'X-Content-Type-Options':'nosniff',
            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            'Referrer-Policy':'no-referrer',
        })
    async def redirect(request):
        raise web.HTTPFound('/app/')
    app.router.add_get('/app', redirect)
    app.router.add_get('/app/', asset)
    app.router.add_get('/app/{name}', asset)
