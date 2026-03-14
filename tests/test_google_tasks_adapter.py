import unittest

from bot.adapters.google_tasks_adapter import GoogleTasksAdapter


class _Resp:
    def __init__(self, status: int, data: dict):
        self.status = status
        self._data = data
        self.headers = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self, content_type=None):
        return self._data


class _Session:
    def post(self, *args, **kwargs):
        return _Resp(401, {"error": "invalid_grant"})


class GoogleTasksAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_auth_failure_does_not_permanently_disable_integration(self) -> None:
        adapter = GoogleTasksAdapter("client-id", "client-secret", "refresh-token")
        adapter._session = _Session()

        self.assertTrue(adapter.enabled())

        with self.assertRaisesRegex(RuntimeError, "authentication failed"):
            await adapter._ensure_token()

        self.assertTrue(adapter.enabled())


if __name__ == "__main__":
    unittest.main()
