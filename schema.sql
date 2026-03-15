-- Database bootstrap for the single-user personal assistant.
--
-- Source of truth: Postgres.
-- Runtime contract:
-- - single-user only
-- - reminders are DB-backed and stateful
-- - LLM actions are persisted as drafts and require confirmation

BEGIN;

CREATE TABLE IF NOT EXISTS projects (
    id SERIAL PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    deadline TIMESTAMP NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
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
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
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
    chat_id BIGINT,
    text TEXT NOT NULL,
    remind_at TIMESTAMP NOT NULL,
    repeat TEXT NOT NULL DEFAULT 'none',
    is_sent BOOLEAN NOT NULL DEFAULT FALSE,
    status TEXT NOT NULL DEFAULT 'pending',
    next_attempt_at_utc TIMESTAMPTZ,
    claimed_at_utc TIMESTAMPTZ,
    claim_token UUID,
    sent_at_utc TIMESTAMPTZ,
    last_attempt_at_utc TIMESTAMPTZ,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    cancelled_at_utc TIMESTAMPTZ,
    error_code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(is_sent, remind_at);
CREATE INDEX IF NOT EXISTS idx_reminders_status_due ON reminders(status, next_attempt_at_utc);
CREATE INDEX IF NOT EXISTS idx_reminders_chat_id ON reminders(chat_id);

CREATE TABLE IF NOT EXISTS user_settings (
    chat_id BIGINT PRIMARY KEY,
    current_project_id INTEGER,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ui_message_id BIGINT,
    ui_screen TEXT NOT NULL DEFAULT 'home',
    ui_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    menu_message_id BIGINT
);

CREATE TABLE IF NOT EXISTS events (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    event_type TEXT NOT NULL DEFAULT '',
    project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    text TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_project_id ON events(project_id);
CREATE INDEX IF NOT EXISTS idx_events_task_id ON events(task_id);

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
    ics_url TEXT DEFAULT '',
    external_uid TEXT DEFAULT '',
    pending_action_id BIGINT
);
CREATE INDEX IF NOT EXISTS idx_icloud_events_sync_status ON icloud_events(sync_status);
CREATE INDEX IF NOT EXISTS idx_icloud_events_retry ON icloud_events(sync_status, retry_count, last_retry_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_icloud_events_external_uid ON icloud_events(external_uid) WHERE external_uid <> '';

CREATE TABLE IF NOT EXISTS conversation_state (
    chat_id BIGINT NOT NULL,
    flow TEXT NOT NULL,
    step TEXT NOT NULL DEFAULT '',
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ,
    PRIMARY KEY (chat_id, flow)
);
CREATE INDEX IF NOT EXISTS idx_conversation_state_expires ON conversation_state(expires_at);

CREATE TABLE IF NOT EXISTS pending_actions (
    id BIGSERIAL PRIMARY KEY,
    chat_id BIGINT NOT NULL,
    kind TEXT NOT NULL,
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_message_id BIGINT,
    fingerprint TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ,
    confirmed_at TIMESTAMPTZ,
    executed_at TIMESTAMPTZ,
    cancelled_at TIMESTAMPTZ,
    failed_at TIMESTAMPTZ,
    last_error TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_pending_actions_chat_status ON pending_actions(chat_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS processed_updates (
    telegram_update_id BIGINT PRIMARY KEY,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS action_journal (
    id BIGSERIAL PRIMARY KEY,
    chat_id BIGINT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    action_key TEXT,
    action_type TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    undo_payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    undone_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_action_journal_action_key ON action_journal(action_key) WHERE action_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_action_journal_chat_created ON action_journal(chat_id, created_at DESC);

CREATE TABLE IF NOT EXISTS llm_recent_actions (
    chat_id BIGINT NOT NULL,
    fingerprint TEXT NOT NULL,
    action TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    pending_action_id BIGINT,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (chat_id, fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_llm_recent_actions_expires ON llm_recent_actions(expires_at);

COMMIT;
