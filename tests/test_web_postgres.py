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
async def database(*, runtime=False):
    conn=await asyncpg.connect(os.environ['TEST_DATABASE_URL'], statement_cache_size=0)
    schema='web_test_'+uuid4().hex
    await conn.execute(f'CREATE SCHEMA {schema}')
    await conn.execute(f'SET search_path TO {schema}')
    if runtime:
        from bot.db.schema import ensure_schema
        await ensure_schema(conn)
        await ensure_schema(conn)
    else:
        await conn.execute(Path('schema.sql').read_text())
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


def test_telegram_deadline_change_rejects_stale_browser_edit():
    async def run():
        from bot.services.task_actions import set_due
        from bot.services.task_browser import list_tasks, update_task, TaskError
        async with database() as (conn,pool):
            pid=await conn.fetchval("INSERT INTO projects(code) VALUES('INBOX') RETURNING id")
            tid=await conn.fetchval("INSERT INTO tasks(project_id,title) VALUES($1,'before') RETURNING id",pid)
            original=(await list_tasks(conn,scope='all',inbox=False,limit=1,cursor=None,chat_id=9))['tasks'][0]
            async with conn.transaction():
                await set_due(conn,task_id=tid,choice='tomorrow',tz_name='Europe/Moscow')
            with pytest.raises(TaskError) as error:
                async with conn.transaction():
                    await update_task(conn,task_id=tid,changes={'title':'stale browser','deadline':None},expected_updated_at=original['updated_at'])
            assert error.value.code == 'task_conflict'
            assert await conn.fetchval('SELECT title FROM tasks WHERE id=$1',tid) == 'before'
            assert await conn.fetchval('SELECT deadline FROM tasks WHERE id=$1',tid) is not None
    asyncio.run(run())


def test_distinct_concurrent_captures_create_inbox_safely():
    async def run():
        from bot.services.capture import save_capture
        async with database() as (conn,pool):
            async def save(text):
                async with pool.acquire() as c:
                    async with c.transaction():
                        return await save_capture(c,chat_id=9,text=text,client_request_id=str(uuid4()),source='test')
            a,b=await asyncio.gather(save('first'),save('second'))
            assert a['task']['id'] != b['task']['id']
            assert await conn.fetchval('SELECT count(*) FROM tasks') == 2
            assert await conn.fetchval("SELECT count(*) FROM projects WHERE code='INBOX'") == 1
    asyncio.run(run())


def test_runtime_bootstrap_is_repeatable_with_task_version_trigger():
    async def run():
        async with database(runtime=True) as (conn,pool):
            pid=await conn.fetchval("INSERT INTO projects(code) VALUES('INBOX') RETURNING id")
            tid=await conn.fetchval("INSERT INTO tasks(project_id,title) VALUES($1,'version') RETURNING id",pid)
            before=await conn.fetchval('SELECT updated_at FROM tasks WHERE id=$1',tid)
            await conn.execute("UPDATE tasks SET title='changed' WHERE id=$1",tid)
            assert await conn.fetchval('SELECT updated_at FROM tasks WHERE id=$1',tid) > before
            assert await conn.fetchval("SELECT count(*) FROM information_schema.tables WHERE table_schema=current_schema() AND table_name IN ('web_sessions','capture_requests')") == 2
    asyncio.run(run())


def test_native_capture_without_model_lands_in_inbox_once():
    async def run():
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch
        from bot.services.native_intake import process_native_capture
        async with database(runtime=True) as (conn,pool):
            capture_id=str(uuid4())
            async def capture():
                with patch('bot.services.native_intake._load_context',
                           AsyncMock(return_value=('lead',None,'INBOX',[],[]))):
                    return await process_native_capture(
                        text='позвонить в банк', deps=SimpleNamespace(llm=None, tz_name='Europe/Moscow'),
                        db_pool=pool, chat_id=9, source='ios', capture_id=capture_id)
            first=await capture()
            assert first['status'] == 'saved' and first['inbox_fallback'] is True
            # A retry after a lost response must return the same task.
            assert await capture() == first
            rows=await conn.fetch("""SELECT t.title FROM tasks t JOIN projects p ON p.id=t.project_id
                WHERE p.code='INBOX' AND t.status='todo'""")
            assert [r['title'] for r in rows] == ['позвонить в банк']
    asyncio.run(run())


def test_native_edit_version_from_list_is_accepted_and_stale_one_rejected():
    async def run():
        from bot.http.companion import _iso_version
        from bot.services.task_browser import update_task, TaskError
        async with database(runtime=True) as (conn,pool):
            project=await conn.fetchval("INSERT INTO projects(code,name,status) VALUES('INBOX','Входящие','active') RETURNING id")
            task_id=await conn.fetchval("INSERT INTO tasks(project_id,title,status,kind) VALUES($1,'черновик','todo','task') RETURNING id",project)
            version=_iso_version(await conn.fetchval('SELECT updated_at FROM tasks WHERE id=$1',task_id))
            async with conn.transaction():
                await update_task(conn,task_id=task_id,changes={'title':'с телефона'},expected_updated_at=version)
            with pytest.raises(TaskError) as stale:
                async with conn.transaction():
                    await update_task(conn,task_id=task_id,changes={'title':'старая копия'},expected_updated_at=version)
            assert stale.value.code == 'task_conflict'
            assert await conn.fetchval('SELECT title FROM tasks WHERE id=$1',task_id) == 'с телефона'
    asyncio.run(run())


def test_today_plan_is_shared_and_removable_for_due_tasks():
    async def run():
        from datetime import datetime, timedelta, timezone
        from zoneinfo import ZoneInfo
        from bot.services.day_plan import set_planned, load_plan, load_candidates, local_today, DayPlanError
        from bot.services.task_browser import list_tasks
        async with database(runtime=True) as (conn,pool):
            tz='Europe/Moscow'; today=local_today(tz)
            project=await conn.fetchval("INSERT INTO projects(code,name,status) VALUES('INBOX','Входящие','active') RETURNING id")
            add=lambda title,deadline=None: conn.fetchval("INSERT INTO tasks(project_id,title,status,kind,deadline) VALUES($1,$2,'todo','task',$3) RETURNING id",project,title,deadline)
            now_naive=datetime.now(timezone.utc).replace(tzinfo=None)
            chosen=await add('выбрано'); due=await add('срок сегодня',datetime.combine(today,datetime.min.time().replace(hour=12),ZoneInfo(tz)).astimezone(timezone.utc).replace(tzinfo=None))
            overdue=await add('просрочено',now_naive-timedelta(days=3)); plain=await add('без срока')
            async with conn.transaction():
                await set_planned(conn,task_id=chosen,planned=True,today=today)
            plan=[r['id'] for r in await load_plan(conn,tz_name=tz,today=today)]
            assert set(plan)=={chosen,due}
            candidates=[r['id'] for r in await load_candidates(conn,tz_name=tz,today=today)]
            assert candidates[0]==overdue and plain in candidates and chosen not in candidates
            # Removing a due-today task keeps it removed for today only.
            async with conn.transaction():
                await set_planned(conn,task_id=due,planned=False,today=today)
            assert [r['id'] for r in await load_plan(conn,tz_name=tz,today=today)]==[chosen]
            listed={t['id']:t for t in (await list_tasks(conn,scope='all',inbox=False,limit=10,cursor=None,tz_name=tz))['tasks']}
            assert listed[chosen]['today_plan'] and listed[chosen]['planned']
            assert not listed[due]['today_plan']
            await conn.execute("UPDATE tasks SET status='done' WHERE id=$1",plain)
            with pytest.raises(DayPlanError):
                async with conn.transaction():
                    await set_planned(conn,task_id=plain,planned=True,today=today)
    asyncio.run(run())


def test_today_and_backlog_endpoints_mark_today_plan():
    async def run():
        import json
        from types import SimpleNamespace
        from bot.http import companion
        from bot.services.day_plan import set_planned, local_today
        async with database(runtime=True) as (conn,pool):
            os.environ['ASSISTANT_API_TOKEN']='app'
            project=await conn.fetchval("INSERT INTO projects(code,name,status) VALUES('INBOX','Входящие','active') RETURNING id")
            ids=[await conn.fetchval("INSERT INTO tasks(project_id,title,status,kind) VALUES($1,$2,'todo','task') RETURNING id",project,f'дело {i}') for i in range(8)]
            async with conn.transaction():
                await set_planned(conn,task_id=ids[-1],planned=True,today=local_today('Europe/Moscow'))
            class Request:
                headers={'Authorization':'Bearer app'}; cookies={}; query={'limit':'500'}
            ctx=SimpleNamespace(deps=SimpleNamespace(db_pool=pool,tz_name='Europe/Moscow',admin_id=9,icloud=None))
            today=json.loads((await companion.handle_today(Request(),ctx)).body)
            assert today['tasks'][0]['id']==ids[-1] and today['tasks'][0]['today_plan'] and today['tasks'][0]['planned']
            assert sum(t['today_plan'] for t in today['tasks'])==1
            backlog=json.loads((await companion.handle_tasks(Request(),ctx)).body)
            assert len(backlog['tasks'])==8
            assert [t['id'] for t in backlog['tasks'] if t['today_plan']]==[ids[-1]]
    asyncio.run(run())


def test_undo_removes_only_a_recent_capture():
    async def run():
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch
        from bot.services.native_intake import process_native_capture
        from bot.services.undo import undo_latest
        async with database(runtime=True) as (conn,pool):
            async def capture(text, cid):
                with patch('bot.services.native_intake._load_context', AsyncMock(return_value=('lead',None,'INBOX',[],[]))):
                    return await process_native_capture(text=text, deps=SimpleNamespace(llm=None,tz_name='Europe/Moscow'),
                                                        db_pool=pool, chat_id=9, source='telegram', capture_id=cid)
            await capture('старая запись','a1')
            await conn.execute("UPDATE action_journal SET created_at=NOW()-interval '1 hour'")
            await capture('свежая запись','a2')
            async with conn.transaction():
                assert (await undo_latest(conn, chat_id=9)) == '↩️ Отменено: свежая запись'
            async with conn.transaction():
                assert (await undo_latest(conn, chat_id=9)) == 'Нечего отменять.'
            titles=[r['title'] for r in await conn.fetch('SELECT title FROM tasks')]
            assert titles == ['старая запись']
    asyncio.run(run())
