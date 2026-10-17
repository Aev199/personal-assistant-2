import asyncio
from types import SimpleNamespace
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


def test_web_assets_do_not_expose_server_files():
    async def run():
        from bot.http.web_app import attach_web_routes
        app=web.Application()
        attach_web_routes(app, SimpleNamespace(deps=SimpleNamespace(db_pool=None)))
        async with TestClient(TestServer(app)) as client:
            r=await client.get('/app/')
            assert r.status == 200 and r.content_type == 'text/html'
            assert r.headers['Cache-Control'] == 'no-store'
            assert (await client.get('/app/schema.sql')).status == 404
            assert (await client.get('/app/../schema.sql')).status == 404
    asyncio.run(run())
