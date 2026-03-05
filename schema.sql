-- Database schema bootstrap for Personal Assistant bot
--
-- Source of truth: Postgres
-- Notes:
-- - Deadlines and remind_at are stored as UTC "naive" timestamps (TIMESTAMP without TZ)
--   to match existing bot logic (NOW() AT TIME ZONE 'UTC').
-- - Obsidian/Vault is a projection layer; not a source of truth.

BEGIN;

CREATE TABLE IF NOT EXISTS projects (
    id SERIAL PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    deadline TIMESTAMP NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_projects_status_created ON projects(status, created_at DESC);

CREATE TABLE IF NOT EXISTS team (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS tasks (
    id BIGSERIAL PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'task',
    assignee_id INTEGER REFERENCES team(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'todo',
    deadline TIMESTAMP NULL,
    parent_task_id BIGINT REFERENCES tasks(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    g_task_id TEXT,
    g_task_list_id TEXT,
    g_task_hash TEXT,
    g_task_synced_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_tasks_project_status ON tasks(project_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_deadline ON tasks(deadline);
CREATE INDEX IF NOT EXISTS idx_tasks_assignee_status ON tasks(assignee_id, status);

CREATE TABLE IF NOT EXISTS reminders (
    id BIGSERIAL PRIMARY KEY,
    text TEXT NOT NULL,
    remind_at TIMESTAMP NOT NULL,
    repeat TEXT NOT NULL DEFAULT 'none',
    is_sent BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(is_sent, remind_at);

CREATE TABLE IF NOT EXISTS user_settings (
    chat_id BIGINT PRIMARY KEY,
    current_project_id INTEGER,
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    ui_message_id BIGINT,
    ui_screen TEXT NOT NULL DEFAULT 'home',
    ui_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    menu_message_id BIGINT
);

CREATE TABLE IF NOT EXISTS events (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    event_type TEXT NOT NULL,
    project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    task_id INTEGER,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_project_created ON events(project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at DESC);

CREATE TABLE IF NOT EXISTS errors (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    where_at TEXT NOT NULL,
    error TEXT NOT NULL,
    traceback TEXT NOT NULL,
    context TEXT
);
CREATE INDEX IF NOT EXISTS idx_errors_created ON errors(created_at DESC);

CREATE TABLE IF NOT EXISTS sync_status (
    name TEXT PRIMARY KEY,
    last_attempt_at TIMESTAMPTZ,
    last_ok_at TIMESTAMPTZ,
    last_error_at TIMESTAMPTZ,
    last_error TEXT,
    last_duration_ms INTEGER
);

CREATE TABLE IF NOT EXISTS g_tasks_lists (
    name TEXT PRIMARY KEY,
    list_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS icloud_events (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    calendar_url TEXT NOT NULL,
    summary TEXT NOT NULL,
    dtstart_utc TIMESTAMPTZ NOT NULL,
    dtend_utc TIMESTAMPTZ NOT NULL,
    description TEXT DEFAULT '',
    location TEXT DEFAULT '',
    sync_status TEXT NOT NULL DEFAULT 'pending',
    retry_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT DEFAULT '',
    last_retry_at TIMESTAMPTZ,
    ics_url TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_icloud_events_sync_status ON icloud_events(sync_status);
CREATE INDEX IF NOT EXISTS idx_icloud_events_retry ON icloud_events(sync_status, retry_count, last_retry_at);

COMMIT;
