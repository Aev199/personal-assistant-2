import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from bot.http import companion


class Request:
    headers={'Authorization':'Bearer app'}
    def __init__(self, query): self.query=query


def test_browse_rejects_invalid_filters_before_database(monkeypatch):
    monkeypatch.setenv('ASSISTANT_API_TOKEN','app')
    ctx=SimpleNamespace(deps=SimpleNamespace(db_pool=object()))
    for query in [{'scope':'bad'}, {'scope':'all','cursor':'-1'}, {'scope':'work','inbox':'bad'}]:
        r=asyncio.run(companion.handle_tasks(Request(query),ctx))
        assert r.status == 400


def test_update_detects_conflict_before_writing():
    async def run():
        from bot.services.task_browser import update_task, TaskError
        class Conn:
            async def fetchrow(self,*args):
                return {'id':1,'title':'newer','status':'todo','kind':'task','project_id':1,
                        'project_code':'A','updated_at':datetime(2026,10,17,tzinfo=timezone.utc)}
            async def execute(self,*args): raise AssertionError('must not overwrite newer task')
        import pytest
        with pytest.raises(TaskError) as e:
            await update_task(Conn(), task_id=1, changes={'title':'old'}, expected_updated_at='2026-10-16T00:00:00+00:00')
        assert e.value.status == 409
        assert e.value.code == 'task_conflict'
    asyncio.run(run())
