import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import aiohttp


@dataclass
class GoogleTasksAuth:
    client_id: str
    client_secret: str
    refresh_token: str


class GoogleTasksAdapter:
    """Minimal Google Tasks API client (one-way sync).

    - Uses OAuth2 refresh token flow.
    - Keeps an aiohttp ClientSession.
    - Provides helpers for tasklists and tasks.
    """

    TOKEN_URL = "https://oauth2.googleapis.com/token"
    API_BASE = "https://tasks.googleapis.com/tasks/v1"

    def __init__(self, client_id: str, client_secret: str, refresh_token: str):
        self.auth = GoogleTasksAuth(client_id, client_secret, refresh_token)
        self._session: Optional[aiohttp.ClientSession] = None
        self._access_token: Optional[str] = None
        self._access_token_exp: float = 0.0

    async def startup(self) -> None:
        if self._session is None:
            timeout = aiohttp.ClientTimeout(total=20)
            self._session = aiohttp.ClientSession(timeout=timeout)

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def enabled(self) -> bool:
        """Check if Google Tasks credentials are configured."""
        return bool(self.auth.client_id and self.auth.client_secret and self.auth.refresh_token)

    async def _ensure_token(self) -> str:
        if not self.enabled():
            raise RuntimeError("Google Tasks is not configured")
        now = asyncio.get_event_loop().time()
        if self._access_token and now < self._access_token_exp - 30:
            return self._access_token

        if self._session is None:
            await self.startup()
        assert self._session is not None

        payload = {
            "client_id": self.auth.client_id,
            "client_secret": self.auth.client_secret,
            "refresh_token": self.auth.refresh_token,
            "grant_type": "refresh_token",
        }
        try:
            async with self._session.post(self.TOKEN_URL, data=payload) as resp:
                data = await resp.json(content_type=None)
                if resp.status in (401, 403):
                    self._access_token = None
                    self._access_token_exp = 0.0
                    raise RuntimeError(f"Google Tasks authentication failed ({resp.status}): {data}")
                if resp.status >= 400:
                    raise RuntimeError(f"Token refresh failed ({resp.status}): {data}")
                token = data.get("access_token")
                expires_in = float(data.get("expires_in", 3600))
                if not token:
                    raise RuntimeError(f"Token refresh response missing access_token: {data}")
                self._access_token = token
                self._access_token_exp = now + expires_in
                return token
        except aiohttp.ClientError as e:
            logging.error("Google Tasks token refresh network error: %s", e)
            raise RuntimeError(f"Token refresh network error: {e}")

    async def _request(self, method: str, path: str, *, params: dict | None = None, json: Any | None = None) -> Any:
        if self._session is None:
            await self.startup()
        assert self._session is not None

        token = await self._ensure_token()
        url = f"{self.API_BASE}{path}"
        headers = {"Authorization": f"Bearer {token}"}

        backoff = 1.0
        for attempt in range(5):
            try:
                async with self._session.request(method, url, params=params, json=json, headers=headers) as resp:
                    if resp.status in (429, 500, 502, 503, 504):
                        retry_after = resp.headers.get("Retry-After")
                        wait_s = float(retry_after) if retry_after else backoff
                        await asyncio.sleep(wait_s)
                        backoff = min(backoff * 2, 10)
                        continue

                    data = await resp.json(content_type=None)
                    if resp.status >= 400:
                        raise RuntimeError(f"Google Tasks error ({resp.status}): {data}")
                    return data
            except aiohttp.ClientError as e:
                logging.warning("Google Tasks network error (%s), retry %d", e, attempt + 1)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10)

        raise RuntimeError("Google Tasks request failed after retries")

    async def list_tasklists(self) -> list[dict]:
        data = await self._request("GET", "/users/@me/lists")
        return data.get("items", []) if isinstance(data, dict) else []

    async def create_tasklist(self, title: str) -> dict:
        return await self._request("POST", "/users/@me/lists", json={"title": title})

    async def create_task(self, list_id: str, title: str, *, notes: str | None = None, due: datetime | None = None) -> dict:
        body: dict[str, Any] = {"title": title}
        if notes:
            body["notes"] = notes
        if due:
            body["due"] = self._fmt_due(due)
        return await self._request("POST", f"/lists/{list_id}/tasks", json=body)

    async def delete_task(self, list_id: str, task_id: str) -> bool:
        if self._session is None:
            await self.startup()
        assert self._session is not None

        token = await self._ensure_token()
        url = f"{self.API_BASE}/lists/{list_id}/tasks/{task_id}"
        headers = {"Authorization": f"Bearer {token}"}

        backoff = 1.0
        for attempt in range(5):
            try:
                async with self._session.delete(url, headers=headers) as resp:
                    if resp.status in (200, 204, 404):
                        return True
                    if resp.status in (429, 500, 502, 503, 504):
                        retry_after = resp.headers.get("Retry-After")
                        wait_s = float(retry_after) if retry_after else backoff
                        await asyncio.sleep(wait_s)
                        backoff = min(backoff * 2, 10)
                        continue
                    data = await resp.text()
                    raise RuntimeError(f"Google Tasks delete error ({resp.status}): {data}")
            except aiohttp.ClientError as e:
                logging.warning("Google Tasks delete network error (%s), retry %d", e, attempt + 1)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10)

        raise RuntimeError("Google Tasks delete failed after retries")

    async def get_task(self, list_id: str, task_id: str) -> dict:
        return await self._request("GET", f"/lists/{list_id}/tasks/{task_id}")

    async def update_task(self, list_id: str, task_id: str, body: dict) -> dict:
        """Full update via PUT.

        Some clients/tenants appear to be flaky with PATCH; PUT tends to be reliable.
        """
        return await self._request("PUT", f"/lists/{list_id}/tasks/{task_id}", json=body)

    async def patch_task(self, list_id: str, task_id: str, *, title: str | None = None, notes: str | None = None, due: datetime | None = None, completed: bool | None = None) -> dict:
        body: dict[str, Any] = {}
        if title is not None:
            body["title"] = title
        if notes is not None:
            body["notes"] = notes
        if due is not None:
            body["due"] = self._fmt_due(due)
        if completed is not None:
            if completed:
                body["status"] = "completed"
                body["completed"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            else:
                body["status"] = "needsAction"
                body["completed"] = None

        # Tasks API doesn't support PATCH everywhere reliably; use PUT with partial? We'll use PATCH endpoint.
        return await self._request("PATCH", f"/lists/{list_id}/tasks/{task_id}", json=body)

    @staticmethod
    def _fmt_due(due: datetime) -> str:
        """Format datetime as RFC3339 UTC (Z) for Google Tasks."""
        if due.tzinfo is None:
            due = due.replace(tzinfo=timezone.utc)
        return due.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
