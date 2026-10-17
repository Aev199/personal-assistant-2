"""Lossless capture, with transaction-scoped retry keys shared by clients."""
import hashlib
import json
from bot.db import db_add_event


class CaptureConflict(Exception):
    pass


async def save_capture(conn, *, chat_id: int, text: str,
                       client_request_id: str | None, source: str) -> dict:
    """Caller owns the transaction, including lock, task and retry receipt."""
    text_hash = hashlib.sha256(text.encode()).hexdigest()
    if client_request_id is not None:
        lock = int.from_bytes(hashlib.sha256(f'capture:{chat_id}:{client_request_id}'.encode()).digest()[:8], 'big', signed=True)
        await conn.execute('SELECT pg_advisory_xact_lock($1::bigint)', lock)
        previous = await conn.fetchrow('''SELECT text_hash, result_json FROM capture_requests
            WHERE chat_id=$1 AND request_id=$2''', chat_id, client_request_id)
        if previous:
            if previous['text_hash'] != text_hash:
                raise CaptureConflict()
            value = previous['result_json']
            return json.loads(value) if isinstance(value, str) else dict(value)
    project_id = await conn.fetchval("""INSERT INTO projects(code,name,status)
        VALUES ('INBOX','Входящие','active')
        ON CONFLICT (code) DO UPDATE SET status='active' RETURNING id""")
    task_id = int(await conn.fetchval('''INSERT INTO tasks (project_id, title, status, kind)
        VALUES ($1, $2, 'todo', 'task') RETURNING id''', int(project_id), text))
    await db_add_event(conn, 'task_created', int(project_id), task_id,
                       f'{source} capture: #{task_id} {text}')
    result = {'ok': True, 'task': {'id': task_id, 'title': text, 'project': 'INBOX'}}
    if client_request_id is not None:
        await conn.execute('''INSERT INTO capture_requests(chat_id, request_id, text_hash, result_json)
            VALUES ($1,$2,$3,$4::jsonb)''', chat_id, client_request_id, text_hash, json.dumps(result, ensure_ascii=False))
    return result
