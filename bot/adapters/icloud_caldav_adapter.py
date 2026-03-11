import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import aiohttp


@dataclass
class ICloudCalDAVAuth:
    apple_id: str
    app_password: str


@dataclass
class ICloudEvent:
    """Represents an iCloud event with sync tracking."""
    summary: str
    dtstart_utc: datetime
    dtend_utc: datetime
    description: str = ""
    location: str = ""
    sync_status: str = "pending"  # pending, synced, sync_failed
    retry_count: int = 0
    last_error: str = ""


class ICloudCalDAVAdapter:
    """
    Minimal iCloud CalDAV client for CREATE-ONLY events.

    Stable mode:
      - calendar collection URL must be provided (e.g. .../calendars/<id>/)
      - uses BasicAuth with Apple ID + app-specific password
    """

    def __init__(self, auth: ICloudCalDAVAuth, timeout_sec: int = 20):
        self._auth = auth
        self._timeout = aiohttp.ClientTimeout(total=timeout_sec)
        self._session: Optional[aiohttp.ClientSession] = None

    async def startup(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def create_event(
        self,
        calendar_url: str,
        summary: str,
        dtstart_utc: datetime,
        dtend_utc: datetime,
        description: str = "",
        location: str = "",
    ) -> tuple[str, bool]:
        """
        Create an event in the given calendar collection URL.
        Returns tuple of (ics_url, success).
        On failure, returns ("", False) to allow caller to handle gracefully.
        """
        if not calendar_url:
            raise ValueError("calendar_url is empty")
        if self._session is None:
            raise RuntimeError("CalDAV session not started")

        # Ensure trailing slash for collection URL
        if not calendar_url.endswith("/"):
            calendar_url += "/"

        if dtstart_utc.tzinfo is None:
            dtstart_utc = dtstart_utc.replace(tzinfo=timezone.utc)
        else:
            dtstart_utc = dtstart_utc.astimezone(timezone.utc)

        if dtend_utc.tzinfo is None:
            dtend_utc = dtend_utc.replace(tzinfo=timezone.utc)
        else:
            dtend_utc = dtend_utc.astimezone(timezone.utc)

        uid = str(uuid.uuid4())
        dtstamp = datetime.now(timezone.utc)

        def fmt(dt: datetime) -> str:
            return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

        ics = (
            "BEGIN:VCALENDAR\r\n"
            "VERSION:2.0\r\n"
            "PRODID:-//personal-assistant//iCloud CalDAV//EN\r\n"
            "CALSCALE:GREGORIAN\r\n"
            "BEGIN:VEVENT\r\n"
            f"UID:{uid}\r\n"
            f"DTSTAMP:{fmt(dtstamp)}\r\n"
            f"DTSTART:{fmt(dtstart_utc)}\r\n"
            f"DTEND:{fmt(dtend_utc)}\r\n"
            f"SUMMARY:{_escape(summary)}\r\n"
        )
        if description:
            ics += f"DESCRIPTION:{_escape(description)}\r\n"
        if location:
            ics += f"LOCATION:{_escape(location)}\r\n"
        ics += (
            "END:VEVENT\r\n"
            "END:VCALENDAR\r\n"
        )

        ics_url = f"{calendar_url}{uid}.ics"
        headers = {
            "Content-Type": "text/calendar; charset=utf-8",
            "If-None-Match": "*",
        }
        auth = aiohttp.BasicAuth(self._auth.apple_id, self._auth.app_password)

        # Retry a few times on transient failures
        for attempt in range(4):
            try:
                async with self._session.put(ics_url, data=ics.encode("utf-8"), headers=headers, auth=auth) as resp:
                    txt = await resp.text()
                    if resp.status in (200, 201, 204):
                        return ics_url, True
                    if resp.status in (409,) and attempt < 3:
                        await asyncio.sleep(0.2 * (attempt + 1))
                        continue
                    # Non-retryable error
                    logging.error(f"CalDAV PUT failed: HTTP {resp.status}: {txt[:300]}")
                    return "", False
            except aiohttp.ClientError as e:
                if attempt < 3:
                    await asyncio.sleep(0.3 * (attempt + 1))
                    continue
                # Final attempt failed
                logging.error(f"CalDAV network error: {e}")
                return "", False
        return ics_url, True

    async def delete_event(self, ics_url: str) -> bool:
        if not ics_url:
            return False
        if self._session is None:
            raise RuntimeError("CalDAV session not started")

        auth = aiohttp.BasicAuth(self._auth.apple_id, self._auth.app_password)
        for attempt in range(4):
            try:
                async with self._session.delete(ics_url, auth=auth) as resp:
                    if resp.status in (200, 204, 404):
                        return True
                    if resp.status in (409, 423) and attempt < 3:
                        await asyncio.sleep(0.2 * (attempt + 1))
                        continue
                    txt = await resp.text()
                    logging.error(f"CalDAV DELETE failed: HTTP {resp.status}: {txt[:300]}")
                    return False
            except aiohttp.ClientError as e:
                if attempt < 3:
                    await asyncio.sleep(0.3 * (attempt + 1))
                    continue
                logging.error(f"CalDAV delete network error: {e}")
                return False
        return False


def _escape(s: str) -> str:
    # Minimal iCalendar escaping
    return (s or "").replace("\\", "\\\\").replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;")
