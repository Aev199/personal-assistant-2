"""Optional transactional tests. TEST_DATABASE_URL must name an isolated test DB.
Each case owns a random schema and never changes public data.
"""
import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
from uuid import uuid4
import asyncpg
import pytest

pytestmark = pytest.mark.skipif(not os.getenv('TEST_DATABASE_URL'), reason='set TEST_DATABASE_URL to a disposable test database')


@asynccontextmanager
async def database():
    conn=await asyncpg.connect(os.environ['TEST_DATABASE_URL'], statement_cache_size=0)
    schema='web_test_'+uuid4().hex
    await conn.execute(f'CREATE SCHEMA {schema}')
    await conn.execute(f'SET search_path TO {schema}')
    sql=Path('schema.sql').read_text()
    await conn.execute(sql)
    async def init(c):
        await c.execute(f'SET search_path TO {schema}')
    pool=await asyncpg.create_pool(os.environ['TEST_DATABASE_URL'], min_size=1,max_size=2,statement_cache_size=0,setup=init)
    try:
        yield conn,pool
    finally:
        await pool.close()
        await conn.execute(f'DROP SCHEMA {schema} CASCADE')
        await conn.close()


def test_capture_retry_conflict_and_rollback():
    async def run():
        from bot.services.capture import save_capture, CaptureConflict
        async with database() as (conn,pool):
            key=str(uuid4())
            async def save(text='hello',request_id=key):
                async with pool.acquire() as c:
                    async with c.transaction():
                        return await save_capture(c,chat_id=9,text=text,client_request_id=request_id,source='test')
            first=await save()
            assert await save() == first
            with pytest.raises(CaptureConflict): await save('different')
            assert await conn.fetchval('SELECT count(*) FROM tasks') == 1
            with pytest.raises(RuntimeError):
                async with conn.transaction():
                    await save_capture(conn,chat_id=9,text='rollback',client_request_id=str(uuid4()),source='test')
                    raise RuntimeError('simulate failure before commit')
            assert await conn.fetchval('SELECT count(*) FROM tasks') == 1
            assert await conn.fetchval('SELECT count(*) FROM capture_requests') == 1
            await save(request_id=None)
            assert await conn.fetchval('SELECT count(*) FROM tasks') == 2
    asyncio.run(run())


def test_concurrent_capture_returns_one_task():
    async def run():
        from bot.services.capture import save_capture
        async with database() as (conn,pool):
            # Pre-existing INBOX isolates retry-key locking from project creation.
            await conn.execute("INSERT INTO projects(code) VALUES('INBOX')")
            key=str(uuid4())
            async def save():
                async with pool.acquire() as c:
                    async with c.transaction():
                        return await save_capture(c,chat_id=9,text='concurrent',client_request_id=key,source='test')
            a,b=await asyncio.gather(save(),save())
            assert a == b
            assert await conn.fetchval('SELECT count(*) FROM tasks') == 1
    asyncio.run(run())


def test_session_storage_expiry_rotation_and_logout():
    async def run():
        from bot.services.web_sessions import create_session,load_session,delete_session,digest
        async with database() as (conn,pool):
            secret,row=await create_session(pool,'token')
            assert (await load_session(pool,secret,'token'))['csrf_token'] == row['csrf_token']
            assert await load_session(pool,secret,'rotated') is None
            assert await conn.fetchval('SELECT session_hash FROM web_sessions') == digest(secret)
            await conn.execute("UPDATE web_sessions SET expires_at=NOW()-interval '1 second'")
            assert await load_session(pool,secret,'token') is None
            secret,_=await create_session(pool,'token')
            await delete_session(pool,secret)
            assert await load_session(pool,secret,'token') is None
    asyncio.run(run())


def test_complete_pagination_and_atomic_edit_triage():
    async def run():
        from bot.services.task_browser import list_tasks, update_task, move_inbox_to_idea, TaskError
        async with database() as (conn,pool):
            pid=await conn.fetchval("INSERT INTO projects(code) VALUES('INBOX') RETURNING id")
            await conn.execute("INSERT INTO tasks(project_id,title) SELECT $1,'work '||n FROM generate_series(1,510) n",pid)
            personal=await conn.fetchval("INSERT INTO tasks(project_id,title,kind) VALUES($1,'personal','personal') RETURNING id",pid)
            p=await list_tasks(conn,scope='personal',inbox=False,limit=200,cursor=None,chat_id=9)
            assert [t['id'] for t in p['tasks']] == [personal]
            ids=[]; cursor=None
            while True:
                p=await list_tasks(conn,scope='work',inbox=False,limit=200,cursor=cursor,chat_id=9)
                ids.extend(t['id'] for t in p['tasks'])
                cursor=p['next_cursor']
                if cursor is None: break
            assert len(ids) == len(set(ids)) == 510
            first=await list_tasks(conn,scope='work',inbox=True,limit=1,cursor=None,chat_id=9)
            task=first['tasks'][0]
            async with conn.transaction():
                edited=await update_task(conn,task_id=task['id'],changes={'title':'edited'},expected_updated_at=task['updated_at'])
            assert edited['updated_at'] != task['updated_at']
            with pytest.raises(TaskError):
                async with conn.transaction():
                    await update_task(conn,task_id=task['id'],changes={'title':'stale'},expected_updated_at=task['updated_at'])
            assert await conn.fetchval('SELECT title FROM tasks WHERE id=$1',task['id']) == 'edited'
            async with conn.transaction():
                idea=await move_inbox_to_idea(conn,chat_id=9,task_id=task['id'])
            assert await conn.fetchval('SELECT text FROM ideas WHERE id=$1',idea) == 'edited'
            assert await conn.fetchval('SELECT status FROM tasks WHERE id=$1',task['id']) == 'done'
            with pytest.raises(TaskError):
                async with conn.transaction(): await move_inbox_to_idea(conn,chat_id=9,task_id=task['id'])
            assert await conn.fetchval('SELECT count(*) FROM ideas') == 1
            second=ids[1]
            with pytest.raises(RuntimeError):
                async with conn.transaction():
                    await move_inbox_to_idea(conn,chat_id=9,task_id=second)
                    raise RuntimeError('rollback triage')
            assert await conn.fetchval('SELECT status FROM tasks WHERE id=$1',second) == 'todo'
            assert await conn.fetchval('SELECT count(*) FROM ideas') == 1
    asyncio.run(run())
