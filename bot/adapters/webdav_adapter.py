import os
import base64
import asyncio
import logging
from urllib.parse import quote
from typing import Optional, Tuple

import aiohttp

logger = logging.getLogger(__name__)


class WebDavAdapter:
    """
    Async WebDAV adapter (Yandex Disk default).

    Goals:
    - Never block forever (timeouts)
    - Reuse one aiohttp.ClientSession (reduces overhead and random socket issues)
    - Retry with backoff for transient errors (429/5xx, timeouts)
    """

    def __init__(self) -> None:
        self.base_url = os.getenv("WEBDAV_BASE_URL", "https://webdav.yandex.ru").rstrip("/")
        login = os.getenv("YANDEX_LOGIN", "")
        password = os.getenv("YANDEX_PASSWORD", "")
        auth_str = f"{login}:{password}"

        self.headers = {
            "Authorization": f"Basic {base64.b64encode(auth_str.encode()).decode()}",
        }

        # Root path inside WebDAV where your Obsidian Vault lives
        self.base_path = os.getenv("VAULT_PATH", "/Obsidian/Vault").rstrip("/")

        self._session: Optional[aiohttp.ClientSession] = None
        total = float(os.getenv("WEBDAV_TIMEOUT_SEC", "10"))
        self._timeout = aiohttp.ClientTimeout(total=total)

        self._max_attempts = int(os.getenv("WEBDAV_RETRIES", "4"))
        self._base_backoff = float(os.getenv("WEBDAV_BACKOFF_SEC", "0.5"))

    async def startup(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers=self.headers, timeout=self._timeout)

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def ping(self) -> bool:
        """Lightweight connectivity check for health endpoint."""
        try:
            if self._session is None or self._session.closed:
                await self.startup()
            assert self._session is not None
            url = self._build_url("/")
            # PROPFIND is commonly supported by WebDAV servers for directory listing.
            # Use Depth: 0 to keep it lightweight.
            async with self._session.request("PROPFIND", url, headers={"Depth": "0"}) as resp:
                await resp.read()
                return resp.status in (200, 204, 207)
        except Exception:
            return False

    def _build_url(self, remote_path: str) -> str:
        if not remote_path.startswith("/"):
            remote_path = "/" + remote_path
        full_path = f"{self.base_path}{remote_path}"
        return self.base_url + quote(full_path, safe="/:")

    async def _req(self, method: str, remote_path: str, data: Optional[bytes] = None) -> Tuple[int, bytes]:
        if self._session is None or self._session.closed:
            await self.startup()

        assert self._session is not None
        url = self._build_url(remote_path)

        retry_statuses = {429, 500, 502, 503, 504}
        last_exc: Optional[Exception] = None

        for attempt in range(1, self._max_attempts + 1):
            try:
                async with self._session.request(method, url, data=data) as resp:
                    body = await resp.read()
                    if resp.status in retry_statuses and attempt < self._max_attempts:
                        await asyncio.sleep(self._base_backoff * (2 ** (attempt - 1)))
                        continue
                    return resp.status, body

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_exc = e
                if attempt < self._max_attempts:
                    await asyncio.sleep(self._base_backoff * (2 ** (attempt - 1)))
                    continue

        if last_exc:
            logger.warning("WebDAV failed: %s %s (%s)", method, url, last_exc)
        return 500, b""

    async def read_file(self, remote_path: str) -> str:
        status, content = await self._req("GET", remote_path)
        if status == 200:
            try:
                return content.decode("utf-8")
            except UnicodeDecodeError:
                return content.decode("utf-8", errors="replace")
        return ""

    async def upload_file(self, remote_path: str, content: str) -> None:
        data = content.encode("utf-8")
        status, _ = await self._req("PUT", remote_path, data=data)
        if status in (200, 201, 204):
            return

        # If missing dirs - create them and retry once
        dir_path = os.path.dirname(remote_path)
        parts = dir_path.strip("/").split("/")
        current = ""
        for part in parts:
            if not part:
                continue
            current += f"/{part}"
            await self._req("MKCOL", current)

        await self._req("PUT", remote_path, data=data)

    async def delete_file(self, remote_path: str) -> None:
        """Delete a remote file.

        WebDAV servers typically support DELETE for files.
        Treat 404 as success (already deleted).
        """
        status, _ = await self._req("DELETE", remote_path)
        if status in (200, 202, 204, 404):
            return
        # Some servers might respond with 207/301 etc for edge cases; ignore hard-fail.
        logger.warning("WebDAV delete returned %s for %s", status, remote_path)
