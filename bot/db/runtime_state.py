"""DB-backed runtime state helpers for idempotency and restart recovery."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg

_RECENT_FALLBACK: dict[tuple[int, str], dict[str, Any]] = {}


def _json_loads(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            raw = json.loads(value)
        except Exception:
            return {}
        return raw if isinstance(raw, dict) else {}
    return {}


async def get_conversation_state(
    conn: asyncpg.Connection,
    chat_id: int,
    flow: str,
) -> dict[str, Any] | None:
    if not hasattr(conn, "fetchrow"):
        return None
    row = await conn.fetchrow(
        """
        SELECT step, payload_json, expires_at
        FROM conversation_state
        WHERE chat_id=$1 AND flow=$2
        """,
        int(chat_id),
        str(flow),
    )
    if not row:
        return None
    expires_at = row["expires_at"]
    if expires_at is not None and expires_at <= datetime.now(timezone.utc):
        await clear_conversation_state(conn, chat_id, flow)
        return None
    return {
        "flow": str(flow),
        "step": str(row["step"] or ""),
        "payload": _json_loads(row["payload_json"]),
        "expires_at": expires_at,
    }


async def set_conversation_state(
    conn: asyncpg.Connection,
    chat_id: int,
    flow: str,
    *,
    step: str,
    payload: dict[str, Any] | None = None,
    ttl_sec: int | None = None,
) -> None:
    if not hasattr(conn, "execute"):
        return
    expires_at = None
    if ttl_sec and ttl_sec > 0:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(ttl_sec))
    await conn.execute(
        """
        INSERT INTO conversation_state(chat_id, flow, step, payload_json, updated_at, expires_at)
        VALUES($1, $2, $3, $4::jsonb, NOW(), $5)
        ON CONFLICT(chat_id, flow) DO UPDATE SET
            step=EXCLUDED.step,
            payload_json=EXCLUDED.payload_json,
            updated_at=NOW(),
            expires_at=EXCLUDED.expires_at
        """,
        int(chat_id),
        str(flow),
        str(step),
        json.dumps(payload or {}, ensure_ascii=False),
        expires_at,
    )


async def clear_conversation_state(conn: asyncpg.Connection, chat_id: int, flow: str) -> None:
    if not hasattr(conn, "execute"):
        return
    await conn.execute(
        "DELETE FROM conversation_state WHERE chat_id=$1 AND flow=$2",
        int(chat_id),
        str(flow),
    )


async def register_processed_update(conn: asyncpg.Connection, update_id: int) -> bool:
    if not hasattr(conn, "execute"):
        return True
    result = await conn.execute(
        """
        INSERT INTO processed_updates(telegram_update_id)
        VALUES($1)
        ON CONFLICT DO NOTHING
        """,
        int(update_id),
    )
    return result.endswith("1")


async def find_recent_action(
    conn: asyncpg.Connection,
    *,
    chat_id: int,
    fingerprint: str,
) -> dict[str, Any] | None:
    if not hasattr(conn, "fetchrow"):
        item = _RECENT_FALLBACK.get((int(chat_id), str(fingerprint)))
        if not item:
            return None
        if item["expires_at"] <= datetime.now(timezone.utc):
            _RECENT_FALLBACK.pop((int(chat_id), str(fingerprint)), None)
            return None
        return dict(item)
    await conn.execute("DELETE FROM llm_recent_actions WHERE expires_at <= NOW()")
    row = await conn.fetchrow(
        """
        SELECT action, summary, pending_action_id, expires_at
        FROM llm_recent_actions
        WHERE chat_id=$1 AND fingerprint=$2
        """,
        int(chat_id),
        str(fingerprint),
    )
    if not row:
        return None
    return {
        "fingerprint": fingerprint,
        "action": str(row["action"] or ""),
        "summary": str(row["summary"] or ""),
        "pending_action_id": row["pending_action_id"],
        "expires_at": row["expires_at"],
    }


async def remember_recent_action(
    conn: asyncpg.Connection,
    *,
    chat_id: int,
    fingerprint: str,
    action: str,
    summary: str,
    pending_action_id: int | None = None,
    ttl_sec: int = 45,
) -> None:
    if not hasattr(conn, "execute"):
        _RECENT_FALLBACK[(int(chat_id), str(fingerprint))] = {
            "fingerprint": str(fingerprint),
            "action": str(action),
            "summary": str(summary),
            "pending_action_id": pending_action_id,
            "expires_at": datetime.now(timezone.utc) + timedelta(seconds=max(15, int(ttl_sec or 45))),
        }
        return
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=max(15, int(ttl_sec or 45)))
    await conn.execute(
        """
        INSERT INTO llm_recent_actions(chat_id, fingerprint, action, summary, pending_action_id, expires_at)
        VALUES($1, $2, $3, $4, $5, $6)
        ON CONFLICT(chat_id, fingerprint) DO UPDATE SET
            action=EXCLUDED.action,
            summary=EXCLUDED.summary,
            pending_action_id=EXCLUDED.pending_action_id,
            expires_at=EXCLUDED.expires_at
        """,
        int(chat_id),
        str(fingerprint),
        str(action),
        str(summary),
        pending_action_id,
        expires_at,
    )


async def forget_recent_action(conn: asyncpg.Connection, *, chat_id: int, fingerprint: str | None) -> None:
    if not hasattr(conn, "execute"):
        if fingerprint:
            _RECENT_FALLBACK.pop((int(chat_id), str(fingerprint)), None)
        return
    if not fingerprint:
        return
    await conn.execute(
        "DELETE FROM llm_recent_actions WHERE chat_id=$1 AND fingerprint=$2",
        int(chat_id),
        str(fingerprint),
    )


async def create_pending_action(
    conn: asyncpg.Connection,
    *,
    chat_id: int,
    kind: str,
    payload: dict[str, Any],
    source_message_id: int | None,
    fingerprint: str | None = None,
    ttl_sec: int = 900,
) -> int:
    if not hasattr(conn, "fetchval"):
        return 0
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=max(60, int(ttl_sec or 900)))
    return int(
        await conn.fetchval(
            """
            INSERT INTO pending_actions(chat_id, kind, payload_json, source_message_id, fingerprint, status, expires_at)
            VALUES($1, $2, $3::jsonb, $4, $5, 'pending', $6)
            RETURNING id
            """,
            int(chat_id),
            str(kind),
            json.dumps(payload or {}, ensure_ascii=False),
            source_message_id,
            str(fingerprint or ""),
            expires_at,
        )
    )


async def get_pending_action(
    conn: asyncpg.Connection,
    *,
    chat_id: int,
    pending_action_id: int,
) -> dict[str, Any] | None:
    if not hasattr(conn, "fetchrow"):
        return None
    row = await conn.fetchrow(
        """
        SELECT id, kind, payload_json, source_message_id, fingerprint, status, expires_at,
               created_at, confirmed_at, executed_at, cancelled_at, failed_at, last_error
        FROM pending_actions
        WHERE id=$1 AND chat_id=$2
        """,
        int(pending_action_id),
        int(chat_id),
    )
    if not row:
        return None
    expires_at = row["expires_at"]
    if expires_at is not None and expires_at <= datetime.now(timezone.utc):
        await conn.execute(
            "UPDATE pending_actions SET status='expired' WHERE id=$1 AND status='pending'",
            int(pending_action_id),
        )
        return None
    return {
        "id": int(row["id"]),
        "kind": str(row["kind"] or ""),
        "payload": _json_loads(row["payload_json"]),
        "source_message_id": row["source_message_id"],
        "fingerprint": str(row["fingerprint"] or ""),
        "status": str(row["status"] or ""),
        "expires_at": row["expires_at"],
        "created_at": row["created_at"],
        "confirmed_at": row["confirmed_at"],
        "executed_at": row["executed_at"],
        "cancelled_at": row["cancelled_at"],
        "failed_at": row["failed_at"],
        "last_error": str(row["last_error"] or ""),
    }


async def mark_pending_action_status(
    conn: asyncpg.Connection,
    *,
    pending_action_id: int,
    status: str,
    last_error: str | None = None,
) -> None:
    if not hasattr(conn, "execute"):
        return
    timestamp_column = {
        "confirmed": "confirmed_at",
        "executed": "executed_at",
        "cancelled": "cancelled_at",
        "failed": "failed_at",
    }.get(str(status), "")
    if timestamp_column:
        await conn.execute(
            f"UPDATE pending_actions SET status=$2, {timestamp_column}=NOW(), last_error=$3 WHERE id=$1",
            int(pending_action_id),
            str(status),
            str(last_error or ""),
        )
        return
    await conn.execute(
        "UPDATE pending_actions SET status=$2, last_error=$3 WHERE id=$1",
        int(pending_action_id),
        str(status),
        str(last_error or ""),
    )


async def record_action_journal(
    conn: asyncpg.Connection,
    *,
    chat_id: int,
    source: str,
    action_type: str,
    summary: str,
    payload: dict[str, Any] | None = None,
    undo_payload: dict[str, Any] | None = None,
    action_key: str | None = None,
) -> int | None:
    if not hasattr(conn, "fetchrow"):
        return None
    row = await conn.fetchrow(
        """
        INSERT INTO action_journal(chat_id, source, action_key, action_type, summary, payload_json, undo_payload_json)
        VALUES($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb)
        ON CONFLICT(action_key) DO NOTHING
        RETURNING id
        """,
        int(chat_id),
        str(source),
        action_key,
        str(action_type),
        str(summary),
        json.dumps(payload or {}, ensure_ascii=False),
        json.dumps(undo_payload or {}, ensure_ascii=False),
    )
    if not row:
        return None
    return int(row["id"])


async def get_latest_undo_action(conn: asyncpg.Connection, *, chat_id: int) -> dict[str, Any] | None:
    if not hasattr(conn, "fetchrow"):
        return None
    row = await conn.fetchrow(
        """
        SELECT id, action_type, summary, payload_json, undo_payload_json
        FROM action_journal
        WHERE chat_id=$1
          AND undone_at IS NULL
          AND undo_payload_json <> '{}'::jsonb
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        int(chat_id),
    )
    if not row:
        return None
    return {
        "id": int(row["id"]),
        "action_type": str(row["action_type"] or ""),
        "summary": str(row["summary"] or ""),
        "payload": _json_loads(row["payload_json"]),
        "undo_payload": _json_loads(row["undo_payload_json"]),
    }


async def mark_action_undone(conn: asyncpg.Connection, journal_id: int) -> None:
    if not hasattr(conn, "execute"):
        return
    await conn.execute(
        "UPDATE action_journal SET undone_at=NOW() WHERE id=$1",
        int(journal_id),
    )
