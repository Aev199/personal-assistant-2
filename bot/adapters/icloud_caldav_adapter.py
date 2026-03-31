import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import aiohttp

logger = logging.getLogger(__name__)


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


@dataclass(frozen=True)
class ICloudVisibleEvent:
    """Represents an event fetched from a calendar collection."""

    calendar_url: str
    summary: str
    dtstart_utc: datetime
    dtend_utc: datetime
    uid: str = ""


class ICloudCalDAVAdapter:
    """
    Minimal iCloud CalDAV client for basic event create/delete/list operations.

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
        uid: str | None = None,
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

        uid = (uid or "").strip() or f"assistant-event-{int(dtstart_utc.timestamp())}-{abs(hash(summary)) % 1_000_000}"
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

    async def list_events(
        self,
        calendar_url: str,
        *,
        start_utc: datetime,
        end_utc: datetime,
    ) -> list[ICloudVisibleEvent]:
        """Fetch visible events for a UTC range from a calendar collection."""
        if not calendar_url:
            return []
        if self._session is None:
            raise RuntimeError("CalDAV session not started")

        if not calendar_url.endswith("/"):
            calendar_url += "/"

        start_utc = _to_utc(start_utc)
        end_utc = _to_utc(end_utc)
        
        logger.info(f"Requesting events from {calendar_url} for range {start_utc} to {end_utc}")

        body = _calendar_query_body(start_utc, end_utc)
        headers = {
            "Depth": "1",
            "Content-Type": "application/xml; charset=utf-8",
        }
        auth = aiohttp.BasicAuth(self._auth.apple_id, self._auth.app_password)

        for attempt in range(4):
            try:
                async with self._session.request(
                    "REPORT",
                    calendar_url,
                    data=body.encode("utf-8"),
                    headers=headers,
                    auth=auth,
                ) as resp:
                    txt = await resp.text()
                    if resp.status in (200, 207):
                        # Log XML response for Bitrix calendar only
                        if "BDFECF73-FFC1-4ADE-AD3B-FB7467C2CA36" in calendar_url:
                            logger.info(f"CalDAV XML response length: {len(txt)} chars")
                            logger.info(f"CalDAV XML response (full): {txt}")
                        return _parse_caldav_multistatus(calendar_url, txt)
                    if resp.status in (409, 423) and attempt < 3:
                        await asyncio.sleep(0.2 * (attempt + 1))
                        continue
                    logging.error("CalDAV REPORT failed: HTTP %s: %s", resp.status, txt[:300])
                    raise RuntimeError(f"CalDAV REPORT failed with HTTP {resp.status}")
            except aiohttp.ClientError as e:
                if attempt < 3:
                    await asyncio.sleep(0.3 * (attempt + 1))
                    continue
                logging.error("CalDAV list network error: %s", e)
                raise
        return []


def _escape(s: str) -> str:
    # Minimal iCalendar escaping
    return (s or "").replace("\\", "\\\\").replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;")


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _fmt_ical_utc(dt: datetime) -> str:
    return _to_utc(dt).strftime("%Y%m%dT%H%M%SZ")


def _calendar_query_body(start_utc: datetime, end_utc: datetime) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
        "<d:prop>"
        "<d:getetag/>"
        "<c:calendar-data/>"
        "</d:prop>"
        "<c:filter>"
        '<c:comp-filter name="VCALENDAR">'
        '<c:comp-filter name="VEVENT">'
        f'<c:time-range start="{_fmt_ical_utc(start_utc)}" end="{_fmt_ical_utc(end_utc)}"/>'
        "</c:comp-filter>"
        "</c:comp-filter>"
        "</c:filter>"
        "</c:calendar-query>"
    )


def _parse_caldav_multistatus(calendar_url: str, xml_text: str) -> list[ICloudVisibleEvent]:
    ns = {
        "d": "DAV:",
        "c": "urn:ietf:params:xml:ns:caldav",
    }
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    # Log response blocks for debugging
    response_blocks = root.findall(".//d:response", ns)
    logger.info(f"Found {len(response_blocks)} response blocks in XML for {calendar_url}")
    
    calendar_data_blocks = root.findall(".//c:calendar-data", ns)
    logger.info(f"Found {len(calendar_data_blocks)} calendar-data blocks in response for {calendar_url}")
    
    events: list[ICloudVisibleEvent] = []
    for idx, calendar_data in enumerate(calendar_data_blocks):
        data = calendar_data.text or ""
        if not data.strip():
            logger.warning(f"calendar-data block {idx} is empty")
            continue
        parsed = _parse_ics_events(calendar_url, data)
        logger.info(f"calendar-data block {idx}: parsed {len(parsed)} events")
        events.extend(parsed)
    
    logger.info(f"Parsed {len(events)} total events from calendar {calendar_url}")
    
    # Дедупликация: по uid (или summary) + время, но БЕЗ calendar_url
    # Это позволяет показывать разные события из одного календаря с одинаковым временем
    deduped: dict[tuple[str, datetime, datetime], ICloudVisibleEvent] = {}
    for event in events:
        key = (event.uid or event.summary, event.dtstart_utc, event.dtend_utc)
        logger.info(f"Adapter event: summary='{event.summary}', uid='{event.uid}', start={event.dtstart_utc}, end={event.dtend_utc}, key={key}")
        if key in deduped:
            logger.warning(f"Adapter: duplicate key {key} - replacing '{deduped[key].summary}' with '{event.summary}'")
        deduped[key] = event
    
    logger.info(f"After adapter deduplication: {len(deduped)} unique events")
    return sorted(deduped.values(), key=lambda item: (item.dtstart_utc, item.dtend_utc, item.summary.lower()))


def _parse_ics_events(calendar_url: str, ics_text: str) -> list[ICloudVisibleEvent]:
    lines = _unfold_ical_lines(ics_text)
    events: list[ICloudVisibleEvent] = []
    in_event = False
    fields: dict[str, tuple[str, dict[str, str]]] = {}

    for line in lines:
        token = line.strip()
        if token == "BEGIN:VEVENT":
            in_event = True
            fields = {}
            continue
        if token == "END:VEVENT":
            in_event = False
            event = _build_visible_event(calendar_url, fields)
            if event is not None:
                events.append(event)
            fields = {}
            continue
        if not in_event or ":" not in line:
            continue
        key_part, value = line.split(":", 1)
        name, params = _parse_ical_property(key_part)
        fields[name] = (value, params)
    return events


def _unfold_ical_lines(ics_text: str) -> list[str]:
    raw_lines = (ics_text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines: list[str] = []
    for raw in raw_lines:
        if not raw:
            continue
        if lines and raw[:1] in {" ", "\t"}:
            lines[-1] += raw[1:]
            continue
        lines.append(raw)
    return lines


def _parse_ical_property(key_part: str) -> tuple[str, dict[str, str]]:
    chunks = [chunk.strip() for chunk in key_part.split(";") if chunk.strip()]
    name = (chunks[0] if chunks else "").upper()
    params: dict[str, str] = {}
    for chunk in chunks[1:]:
        if "=" not in chunk:
            continue
        param_key, param_value = chunk.split("=", 1)
        params[param_key.upper()] = param_value
    return name, params


def _build_visible_event(
    calendar_url: str,
    fields: dict[str, tuple[str, dict[str, str]]],
) -> ICloudVisibleEvent | None:
    dtstart_raw, dtstart_params = fields.get("DTSTART", ("", {}))
    dtend_raw, dtend_params = fields.get("DTEND", ("", {}))
    summary = str(fields.get("SUMMARY", ("", {}))[0] or "").strip() or "Без названия"
    uid = str(fields.get("UID", ("", {}))[0] or "").strip()
    dtstart_utc = _parse_ical_datetime(dtstart_raw, dtstart_params)
    dtend_utc = _parse_ical_datetime(dtend_raw, dtend_params)
    if dtstart_utc is None:
        logging.warning(f"Skipping event '{summary}' - no valid DTSTART: {dtstart_raw}")
        return None
    if dtend_utc is None or dtend_utc <= dtstart_utc:
        dtend_utc = dtstart_utc
    return ICloudVisibleEvent(
        calendar_url=calendar_url,
        summary=summary,
        dtstart_utc=dtstart_utc,
        dtend_utc=dtend_utc,
        uid=uid,
    )


def _parse_ical_datetime(value: str, params: dict[str, str]) -> datetime | None:
    raw = (value or "").strip()
    if not raw:
        return None

    value_kind = str(params.get("VALUE") or "").upper()
    tzid = str(params.get("TZID") or "").strip()

    if value_kind == "DATE" or (len(raw) == 8 and raw.isdigit()):
        try:
            local_dt = datetime.strptime(raw, "%Y%m%d")
        except ValueError:
            return None
        if tzid:
            try:
                return local_dt.replace(tzinfo=ZoneInfo(tzid)).astimezone(timezone.utc)
            except Exception:
                return local_dt.replace(tzinfo=timezone.utc)
        return local_dt.replace(tzinfo=timezone.utc)

    if raw.endswith("Z"):
        try:
            return datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    if len(raw) == 15 and "T" in raw:
        try:
            local_dt = datetime.strptime(raw, "%Y%m%dT%H%M%S")
        except ValueError:
            return None
        if tzid:
            try:
                return local_dt.replace(tzinfo=ZoneInfo(tzid)).astimezone(timezone.utc)
            except Exception:
                return local_dt.replace(tzinfo=timezone.utc)
        return local_dt.replace(tzinfo=timezone.utc)

    return None
