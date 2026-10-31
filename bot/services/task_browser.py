"""Complete browser lists and atomic task edits, using existing task identities."""
from datetime import datetime, timezone
from bot.db import db_add_event
from bot.services.day_plan import local_today, today_plan_sql


class TaskError(Exception):
    def __init__(self, code: str, status: int):
        self.code, self.status = code, status
        super().__init__(code)


def iso(value):
    if value is None: return None
    if value.tzinfo is None: value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


async def list_tasks(conn, *, scope: str, inbox: bool, limit: int, cursor: int | None, chat_id: int = 0,
                     tz_name: str = 'Europe/Moscow') -> dict:
    if scope not in ('all','work','personal') or not 1 <= limit <= 200 or (cursor is not None and cursor < 0):
        raise TaskError('invalid_filters', 400)
    rows = await conn.fetch('''
        SELECT t.id,t.title,t.deadline,t.status,t.kind,t.updated_at,p.code AS project,
               COALESCE(tm.name,'') AS assignee,
               {plan} AS today_plan, COALESCE(t.planned_for=$5::date,FALSE) AS planned
        FROM tasks t JOIN projects p ON p.id=t.project_id
        LEFT JOIN team tm ON tm.id=t.assignee_id
        WHERE t.status NOT IN ('done','postponed') AND t.kind != 'super'
          AND p.status IN ('active','system') AND t.id > $1
          AND ($2='all' OR ($2='personal' AND t.kind='personal') OR ($2='work' AND t.kind!='personal'))
          AND (NOT $3 OR (p.code='INBOX' AND t.kind='task'))
        ORDER BY t.id ASC LIMIT $4
        '''.replace('{plan}', today_plan_sql('$5', '$6')), cursor or 0, scope, inbox, limit+1,
        local_today(tz_name), tz_name)
    tasks=[]
    for r in rows[:limit]:
        tasks.append({'id':int(r['id']),'title':r['title'],'kind':r['kind'],
                      'project':'' if r['kind']=='personal' else r['project'],
                      'status':r['status'],'assignee':r['assignee'],
                      'today_plan':bool(r['today_plan']),'planned':bool(r['planned']),
                      'deadline':iso(r['deadline'].replace(microsecond=0)) if r['deadline'] else None,'updated_at':iso(r['updated_at'])})
    return {'ok':True,'tasks':tasks,'next_cursor':int(rows[limit-1]['id']) if len(rows)>limit else None}


async def update_task(conn, *, task_id: int, changes: dict, expected_updated_at: str | None) -> dict:
    row=await conn.fetchrow('''SELECT t.id,t.title,t.status,t.kind,t.project_id,t.updated_at,p.code AS project_code
        FROM tasks t JOIN projects p ON p.id=t.project_id WHERE t.id=$1 FOR UPDATE OF t''',task_id)
    if not row: raise TaskError('task_not_found',404)
    if row['kind']=='super' or row['status'] in ('done','postponed'):
        raise TaskError('task_not_active',409)
    if expected_updated_at is not None:
        try:
            expected=datetime.fromisoformat(expected_updated_at.replace('Z','+00:00'))
            if expected.tzinfo is None: raise ValueError()
        except (ValueError,AttributeError,TypeError):
            raise TaskError('invalid_version',400)
        if expected.astimezone(timezone.utc).isoformat() != datetime.fromisoformat(iso(row['updated_at'])).astimezone(timezone.utc).isoformat():
            raise TaskError('task_conflict',409)
    values=[task_id]; assignments=[]
    project_id=int(row['project_id'])
    for key in ('title','deadline','project_code'):
        if key not in changes: continue
        value=changes[key]
        column=key
        if key=='project_code':
            if row['kind']=='personal': raise TaskError('personal_task_has_no_project',409)
            project=await conn.fetchrow("SELECT id FROM projects WHERE status='active' AND UPPER(code)=UPPER($1) LIMIT 1",value)
            if not project: raise TaskError('project_not_found',404)
            column='project_id';value=project_id=int(project['id'])
        values.append(value);assignments.append(f'{column}=${len(values)}')
    if not assignments: raise TaskError('nothing_to_update',400)
    version=await conn.fetchval('UPDATE tasks SET '+','.join(assignments)+',updated_at=clock_timestamp() WHERE id=$1 RETURNING updated_at',*values)
    await db_add_event(conn,'task_updated',project_id,task_id,'web edit')
    return {'ok':True,'task_id':task_id,'status':'updated','updated_at':iso(version)}


async def move_inbox_to_idea(conn, *, chat_id: int, task_id: int) -> int:
    row=await conn.fetchrow('''SELECT t.title,t.kind,t.status,t.project_id,p.code FROM tasks t
        JOIN projects p ON p.id=t.project_id WHERE t.id=$1 FOR UPDATE OF t''',task_id)
    if not row: raise TaskError('task_not_found',404)
    if row['code']!='INBOX' or row['kind']!='task' or row['status'] in ('done','postponed'):
        raise TaskError('not_an_inbox_task',409)
    idea_id=int(await conn.fetchval("INSERT INTO ideas(chat_id,text,source) VALUES($1,$2,'web-inbox') RETURNING id",chat_id,row['title']))
    await conn.execute("UPDATE tasks SET status='done',updated_at=clock_timestamp() WHERE id=$1",task_id)
    await db_add_event(conn,'task_converted_to_idea',int(row['project_id']),task_id,f'idea #{idea_id}')
    return idea_id
