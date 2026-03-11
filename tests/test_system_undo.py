import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.handlers.system import msg_undo_last


class _Acquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Pool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _Acquire(self._conn)


class _Conn:
    pass


class SystemUndoTests(unittest.IsolatedAsyncioTestCase):
    async def test_undo_last_personal_task_deletes_gtask_and_clears_payload(self) -> None:
        conn = _Conn()
        pool = _Pool(conn)
        payload_state = {
            "ui_payload": {
                "undo": {
                    "type": "llm_create",
                    "action": "personal_task",
                    "list_id": "personal-list",
                    "g_task_id": "gt-1",
                    "title": "Buy filter",
                    "fingerprint": "fp-1",
                    "exp": 9999999999,
                },
                "llm_recent": [
                    {"fingerprint": "fp-1", "action": "personal_task", "summary": "Buy filter", "exp": 9999999999}
                ],
            },
            "ui_screen": "home",
            "ui_message_id": None,
        }

        async def _ui_get_state(_conn, _chat_id):
            return payload_state

        async def _ui_set_state(_conn, _chat_id, **kwargs):
            if "ui_payload" in kwargs and kwargs["ui_payload"] is not None:
                payload_state["ui_payload"] = kwargs["ui_payload"]

        deps = SimpleNamespace(
            admin_id=None,
            gtasks=SimpleNamespace(enabled=lambda: True, delete_task=AsyncMock(return_value=True)),
            icloud=None,
            vault=None,
            tz_name="Europe/Moscow",
        )
        state = AsyncMock()
        message = SimpleNamespace(chat=SimpleNamespace(id=42))

        with (
            patch("bot.handlers.system.ui_get_state", _ui_get_state),
            patch("bot.handlers.system.ui_set_state", _ui_set_state),
            patch("bot.handlers.system.try_delete_user_message", AsyncMock()),
            patch("bot.handlers.system.cleanup_main_menu_anchor", AsyncMock()),
            patch("bot.handlers.system.ui_render_home", AsyncMock(return_value=1)),
            patch("bot.handlers.system.ensure_main_menu", AsyncMock()),
            patch("bot.handlers.system.db_add_event", AsyncMock()),
            patch("bot.handlers.system.db_log_error", AsyncMock()),
        ):
            await msg_undo_last(message, state, deps, pool)

        deps.gtasks.delete_task.assert_awaited_once_with("personal-list", "gt-1")
        self.assertNotIn("undo", payload_state["ui_payload"])
        self.assertEqual(payload_state["ui_payload"].get("llm_recent"), [])


if __name__ == "__main__":
    unittest.main()
