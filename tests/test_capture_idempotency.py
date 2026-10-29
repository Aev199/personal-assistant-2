import asyncio
import json
from uuid import uuid4


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
