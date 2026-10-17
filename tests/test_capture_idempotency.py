import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import patch
from bot.http.companion import handle_capture


class Request:
    headers={'Authorization':'Bearer app'}
    def __init__(self, payload): self.payload=payload
    async def json(self): return self.payload


def test_capture_rejects_non_object_json(monkeypatch):
    monkeypatch.setenv('ASSISTANT_API_TOKEN','app')
    ctx=SimpleNamespace(deps=SimpleNamespace(db_pool=object()))
    for data in [[], 'text', 1, None]:
        response=asyncio.run(handle_capture(Request(data),ctx))
        assert response.status == 400


def test_capture_invalid_key_is_rejected_before_database(monkeypatch):
    monkeypatch.setenv('ASSISTANT_API_TOKEN','app')
    ctx=SimpleNamespace(deps=SimpleNamespace(db_pool=object()))
    for key in ['', 'bad', 33, []]:
        r=asyncio.run(handle_capture(Request({'text':'hello','client_request_id':key}),ctx))
        assert r.status == 400


def test_capture_length_and_empty_text(monkeypatch):
    monkeypatch.setenv('ASSISTANT_API_TOKEN','app')
    ctx=SimpleNamespace(deps=SimpleNamespace(db_pool=object()))
    for text, status in [('',400),(' '*2,400),('x'*2001,413),([],400)]:
        r=asyncio.run(handle_capture(Request({'text':text}),ctx))
        assert r.status == status


def test_capture_reuses_saved_result_and_rejects_key_reuse():
    async def run():
        from bot.services.capture import save_capture, CaptureConflict
        class Conn:
            async def execute(self, *args): pass
            async def fetchrow(self, *args):
                return {'text_hash':__import__('hashlib').sha256(b'hello').hexdigest(),
                        'result_json':json.dumps({'ok':True,'task':{'id':7,'title':'hello','project':'INBOX'}})}
        c=Conn()
        result=await save_capture(c,chat_id=1,text='hello',client_request_id=str(uuid4()),source='test')
        assert result['task']['id'] == 7
        import pytest
        with pytest.raises(CaptureConflict):
            await save_capture(c,chat_id=1,text='changed',client_request_id=str(uuid4()),source='test')
    asyncio.run(run())
