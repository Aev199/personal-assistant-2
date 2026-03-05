"""SPA state storage.

The "Ultimate SPA" design keeps a single editable UI message per chat. We store
its message id + current screen + small payload (toasts/undo) in user_settings.
"""

from __future__ import annotations

import json
import time

import asyncpg


async def ui_get_state(conn: asyncpg.Connection, chat_id: int) -> dict:
    row = await conn.fetchrow(
        "SELECT ui_message_id, ui_screen, ui_payload FROM user_settings WHERE chat_id=$1",
        chat_id,
    )
    if not row:
        return {"ui_message_id": None, "ui_screen": "home", "ui_payload": {}}

    payload = row["ui_payload"] or {}
    # asyncpg may return str for json depending on driver versions/settings
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {}

    return {
        "ui_message_id": row["ui_message_id"],
        "ui_screen": row["ui_screen"] or "home",
        "ui_payload": payload if isinstance(payload, dict) else {},
    }


async def ui_set_state(
    conn: asyncpg.Connection,
    chat_id: int,
    *,
    ui_message_id: int | None = None,
    ui_screen: str | None = None,
    ui_payload: dict | None = None,
) -> None:
    # Upsert row first (keeps current_project_id untouched)
    await conn.execute(
        "INSERT INTO user_settings(chat_id, updated_at) VALUES($1, NOW()) "
        "ON CONFLICT(chat_id) DO UPDATE SET updated_at=NOW()",
        chat_id,
    )
    if ui_message_id is not None:
        await conn.execute(
            "UPDATE user_settings SET ui_message_id=$2, updated_at=NOW() WHERE chat_id=$1",
            chat_id,
            ui_message_id,
        )
    if ui_screen is not None:
        await conn.execute(
            "UPDATE user_settings SET ui_screen=$2, updated_at=NOW() WHERE chat_id=$1",
            chat_id,
            ui_screen,
        )
    if ui_payload is not None:
        await conn.execute(
            "UPDATE user_settings SET ui_payload=$2::jsonb, updated_at=NOW() WHERE chat_id=$1",
            chat_id,
            json.dumps(ui_payload, ensure_ascii=False),
        )


# --- UI payload helpers (for undo/toasts) ---

def _ui_payload_get(state: dict) -> dict:
    p = state.get("ui_payload") or {}
    return p if isinstance(p, dict) else {}


def _ui_payload_with(payload: dict, **updates) -> dict:
    p = dict(payload or {})
    p.update(updates)
    return p


def _now_ts() -> int:
    return int(time.time())


def ui_payload_with_toast(payload: dict, text: str, ttl_sec: int = 25) -> dict:
    p = dict(payload or {})
    p["toast"] = {
        "text": str(text or "").strip(),
        "exp": _now_ts() + max(1, int(ttl_sec or 25)),
    }
    return p


def ui_payload_take_toast(payload: dict) -> tuple[str | None, dict]:
    p = dict(payload or {})
    toast = p.get("toast")
    if not isinstance(toast, dict):
        p.pop("toast", None)
        return None, p

    exp = int(toast.get("exp") or 0)
    text = str(toast.get("text") or "").strip()
    p.pop("toast", None)
    if exp and exp < _now_ts():
        return None, p
    if not text:
        return None, p
    return text, p


def _undo_active(payload: dict, *, task_id: int | None = None) -> dict | None:
    undo = (payload or {}).get("undo")
    if not isinstance(undo, dict):
        return None
    exp = int(undo.get("exp") or 0)
    if exp and exp < _now_ts():
        return None
    if task_id is not None and int(undo.get("task_id") or 0) != int(task_id):
        return None
    if undo.get("type") != "task_status":
        return None
    return undo
