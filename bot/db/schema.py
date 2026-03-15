"""Database schema bootstrap.

The project historically relied on pre-provisioned tables. For fresh DB
deployments (Neon/Supabase/Render), missing core tables caused startup crashes.

This bootstrap is best-effort and intentionally conservative: it only creates
tables/columns when missing.
"""

from __future__ import annotations

import asyncpg


async def _column_data_type(conn: asyncpg.Connection, table: str, column: str) -> str | None:
    try:
        return await conn.fetchval(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=$1 AND column_name=$2",
            table,
            column,
        )
    except Exception:
        return None


async def _try_migrate_timestamptz_to_timestamp_utc(conn: asyncpg.Connection, table: str, column: str) -> None:
    """Best-effort migration: TIMESTAMPTZ -> TIMESTAMP (UTC naive).

    Project convention is to store deadlines/reminders as UTC-naive TIMESTAMP.
    Some historical deployments created these columns as TIMESTAMPTZ.

    If the column is TIMESTAMPTZ, we convert it to TIMESTAMP using
    `col AT TIME ZONE 'UTC'` (preserving the same instant).
    """

    dtype = await _column_data_type(conn, table, column)
    if dtype != "timestamp with time zone":
        return
    try:
        await conn.execute(
            f"ALTER TABLE {table} ALTER COLUMN {column} TYPE TIMESTAMP USING ({column} AT TIME ZONE 'UTC')"
        )
    except Exception:
        # Non-fatal: if migration fails, runtime will adapt.
        return


async def ensure_schema(conn: asyncpg.Connection) -> None:
    """Create/patch core schema (best-effort)."""

    # NOTE: There is also a schema.sql at repo root.
    # This bootstrap should stay compatible with the SQL used across the codebase.

    # projects
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS projects (
            id SERIAL PRIMARY KEY,
            code TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            deadline TIMESTAMP NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_projects_status_created ON projects(status, created_at DESC)")

    # team (required before tasks due to FK)
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS team (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            role TEXT NOT NULL DEFAULT ''
        )
        """
    )

    # tasks
    await conn.execute(
        """
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
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    # allow unassigned
    try:
        await conn.execute("ALTER TABLE tasks ALTER COLUMN assignee_id DROP NOT NULL")
    except Exception:
        pass

    # Ensure columns used by handlers exist (best-effort for existing DBs)
    try:
        await conn.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS parent_task_id BIGINT")
    except Exception:
        pass
    try:
        await conn.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS kind TEXT")
        await conn.execute("UPDATE tasks SET kind='task' WHERE kind IS NULL")
        await conn.execute("ALTER TABLE tasks ALTER COLUMN kind SET DEFAULT 'task'")
        await conn.execute("ALTER TABLE tasks ALTER COLUMN kind SET NOT NULL")
    except Exception:
        pass

    await conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_project_status ON tasks(project_id, status)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_deadline ON tasks(deadline)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_assignee_status ON tasks(assignee_id, status)")

    # Best-effort compatibility: migrate TIMESTAMPTZ columns to TIMESTAMP (UTC naive).
    # If this fails, the runtime has additional safeguards.
    await _try_migrate_timestamptz_to_timestamp_utc(conn, "tasks", "deadline")

    # events (history)
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id BIGSERIAL PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            event_type TEXT NOT NULL DEFAULT '',
            project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
            task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
            text TEXT NOT NULL DEFAULT ''
        )
        """
    )
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_events_project_id ON events(project_id)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_events_task_id ON events(task_id)")

    # user_settings
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_settings (
            chat_id BIGINT PRIMARY KEY,
            current_project_id INTEGER,
            menu_message_id BIGINT,
            ui_message_id BIGINT,
            ui_screen TEXT NOT NULL DEFAULT 'home',
            ui_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )

    # reminders
    await conn.execute(
        """
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
        )
        """
    )

    await _try_migrate_timestamptz_to_timestamp_utc(conn, "reminders", "remind_at")

    # projects.deadline is rarely used but keep it consistent.
    await _try_migrate_timestamptz_to_timestamp_utc(conn, "projects", "deadline")

    # Compatibility for DBs created with older/experimental schema.
    # The runtime inserts into (text, remind_at, repeat) and selects by is_sent.
    for stmt in (
        "ALTER TABLE reminders ADD COLUMN IF NOT EXISTS repeat TEXT NOT NULL DEFAULT 'none'",
        "ALTER TABLE reminders ADD COLUMN IF NOT EXISTS is_sent BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE reminders ADD COLUMN IF NOT EXISTS chat_id BIGINT",
        "ALTER TABLE reminders ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'pending'",
        "ALTER TABLE reminders ADD COLUMN IF NOT EXISTS next_attempt_at_utc TIMESTAMPTZ",
        "ALTER TABLE reminders ADD COLUMN IF NOT EXISTS claimed_at_utc TIMESTAMPTZ",
        "ALTER TABLE reminders ADD COLUMN IF NOT EXISTS claim_token UUID",
        "ALTER TABLE reminders ADD COLUMN IF NOT EXISTS sent_at_utc TIMESTAMPTZ",
        "ALTER TABLE reminders ADD COLUMN IF NOT EXISTS last_attempt_at_utc TIMESTAMPTZ",
        "ALTER TABLE reminders ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE reminders ADD COLUMN IF NOT EXISTS cancelled_at_utc TIMESTAMPTZ",
        "ALTER TABLE reminders ADD COLUMN IF NOT EXISTS error_code TEXT",
        # Older bootstrap versions had chat_id/task_id with NOT NULL constraints.
        "ALTER TABLE reminders ALTER COLUMN chat_id DROP NOT NULL",
        "ALTER TABLE reminders ALTER COLUMN task_id DROP NOT NULL",
    ):
        try:
            await conn.execute(stmt)
        except Exception:
            pass
    try:
        await conn.execute("UPDATE reminders SET status='sent' WHERE is_sent = TRUE AND status = 'pending'")
    except Exception:
        pass
    try:
        await conn.execute("UPDATE reminders SET next_attempt_at_utc = remind_at AT TIME ZONE 'UTC' WHERE next_attempt_at_utc IS NULL")
    except Exception:
        pass
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(is_sent, remind_at)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_reminders_status_due ON reminders(status, next_attempt_at_utc)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_reminders_chat_id ON reminders(chat_id)")

    # sync status
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sync_status (
            name TEXT PRIMARY KEY,
            last_attempt_at TIMESTAMPTZ,
            last_ok_at TIMESTAMPTZ,
            last_error_at TIMESTAMPTZ,
            last_error TEXT,
            last_duration_ms INTEGER
        )
        """
    )

    # Google Tasks mappings
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS g_tasks_lists (
            name TEXT PRIMARY KEY,
            list_id TEXT NOT NULL
        )
        """
    )
    try:
        await conn.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS g_task_id TEXT")
        await conn.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS g_task_list_id TEXT")
        await conn.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS g_task_hash TEXT")
        await conn.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS g_task_synced_at TIMESTAMPTZ")
    except Exception:
        pass

    # errors
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS errors (
            id BIGSERIAL PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            where_at TEXT NOT NULL,
            error TEXT NOT NULL,
            traceback TEXT NOT NULL,
            context TEXT
        )
        """
    )
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_errors_created ON errors(created_at DESC)")

    # iCloud events tracking
    await conn.execute(
        """
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
        )
        """
    )
    for stmt in (
        "ALTER TABLE icloud_events ADD COLUMN IF NOT EXISTS external_uid TEXT DEFAULT ''",
        "ALTER TABLE icloud_events ADD COLUMN IF NOT EXISTS pending_action_id BIGINT",
    ):
        try:
            await conn.execute(stmt)
        except Exception:
            pass
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_icloud_events_sync_status ON icloud_events(sync_status)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_icloud_events_retry ON icloud_events(sync_status, retry_count, last_retry_at)")
    await conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_icloud_events_external_uid ON icloud_events(external_uid) WHERE external_uid <> ''")

    # Conversation state for restart-safe followups and bulk flows
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS conversation_state (
            chat_id BIGINT NOT NULL,
            flow TEXT NOT NULL,
            step TEXT NOT NULL DEFAULT '',
            payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at TIMESTAMPTZ,
            PRIMARY KEY (chat_id, flow)
        )
        """
    )
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_conversation_state_expires ON conversation_state(expires_at)")

    # Draft actions produced by LLM and confirmed explicitly by user
    await conn.execute(
        """
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
        )
        """
    )
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_pending_actions_chat_status ON pending_actions(chat_id, status, created_at DESC)")

    # Update deduplication for Telegram polling/webhook ingest
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS processed_updates (
            telegram_update_id BIGINT PRIMARY KEY,
            processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )

    # Executed actions journal used for undo and callback dedupe
    await conn.execute(
        """
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
        )
        """
    )
    await conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_action_journal_action_key ON action_journal(action_key) WHERE action_key IS NOT NULL")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_action_journal_chat_created ON action_journal(chat_id, created_at DESC)")

    # Restart-safe recent LLM dedupe
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS llm_recent_actions (
            chat_id BIGINT NOT NULL,
            fingerprint TEXT NOT NULL,
            action TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL DEFAULT '',
            pending_action_id BIGINT,
            expires_at TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (chat_id, fingerprint)
        )
        """
    )
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_recent_actions_expires ON llm_recent_actions(expires_at)")
