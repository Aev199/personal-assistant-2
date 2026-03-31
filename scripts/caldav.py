#!/usr/bin/env python3
import sys
import argparse
import requests
import xml.etree.ElementTree as ET
from urllib.parse import urljoin

NS = {
    "d": "DAV:",
    "c": "urn:ietf:params:xml:ns:caldav",
}

HEADERS = {
    "Content-Type": "application/xml; charset=utf-8",
    "User-Agent": "icloud-caldav-discovery/1.0",
}

BASE_URL = "https://caldav.icloud.com/"


def extract_hrefs(xml_text):
    root = ET.fromstring(xml_text)
    return [elem.text.strip() for elem in root.findall(".//d:href", NS) if elem.text]


def extract_first(root, xpath):
    elem = root.find(xpath, NS)
    return elem.text.strip() if elem is not None and elem.text else None


def propfind(session, url, body, depth="0"):
    headers = dict(HEADERS)
    headers["Depth"] = depth
    resp = session.request("PROPFIND", url, data=body.encode("utf-8"), headers=headers)
    resp.raise_for_status()
    return resp.text


def get_current_user_principal(session):
    body = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:">
  <d:prop>
    <d:current-user-principal/>
  </d:prop>
</d:propfind>"""
    xml_text = propfind(session, BASE_URL, body, depth="0")
    root = ET.fromstring(xml_text)
    href = extract_first(root, ".//d:current-user-principal/d:href")
    if not href:
        raise RuntimeError("Не удалось получить current-user-principal")
    return urljoin(BASE_URL, href)


def get_calendar_home_set(session, principal_url):
    body = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop>
    <c:calendar-home-set/>
  </d:prop>
</d:propfind>"""
    xml_text = propfind(session, principal_url, body, depth="0")
    root = ET.fromstring(xml_text)
    href = extract_first(root, ".//c:calendar-home-set/d:href")
    if not href:
        raise RuntimeError("Не удалось получить calendar-home-set")
    return urljoin(BASE_URL, href)


def list_calendars(session, calendar_home_url):
    body = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop>
    <d:displayname/>
    <d:resourcetype/>
    <c:supported-calendar-component-set/>
  </d:prop>
</d:propfind>"""
    xml_text = propfind(session, calendar_home_url, body, depth="1")
    root = ET.fromstring(xml_text)

    calendars = []

    for response in root.findall(".//d:response", NS):
        href = extract_first(response, "./d:href")
        displayname = extract_first(response, ".//d:displayname")

        # Проверяем, что это именно calendar collection
        has_calendar = response.find(".//d:resourcetype/c:calendar", NS) is not None
        if not has_calendar:
            continue

        if not href:
            continue

        full_url = urljoin(BASE_URL, href)
        calendars.append({
            "name": displayname or "(без названия)",
            "href": href,
            "url": full_url,
        })

    # убрать дубликаты
    seen = set()
    unique = []
    for cal in calendars:
        key = cal["url"]
        if key not in seen:
            seen.add(key)
            unique.append(cal)

    return unique


def main():
    parser = argparse.ArgumentParser(
        description="Discover iCloud CalDAV calendar URLs"
    )
    parser.add_argument("--apple-id", required=True, help="Apple ID / iCloud email")
    parser.add_argument("--app-password", required=True, help="App-specific password")
    parser.add_argument(
        "--match",
        help="Показать только календари, имя которых содержит эту строку",
    )
    args = parser.parse_args()

    session = requests.Session()
    session.auth = (args.apple_id, args.app_password)

    try:
        principal_url = get_current_user_principal(session)
        print(f"Principal URL: {principal_url}")

        home_url = get_calendar_home_set(session, principal_url)
        print(f"Calendar home: {home_url}")

        calendars = list_calendars(session, home_url)
        if not calendars:
            print("Календари не найдены")
            sys.exit(1)

        if args.match:
            filtered = [
                c for c in calendars
                if args.match.lower() in c["name"].lower()
            ]
        else:
            filtered = calendars

        if not filtered:
            print(f'Нет календарей, подходящих под фильтр: "{args.match}"')
            sys.exit(2)

        print("\nCalendars:")
        for idx, cal in enumerate(filtered, 1):
            print(f"{idx}. {cal['name']}")
            print(f"   URL: {cal['url']}")
            print()

    except requests.HTTPError as e:
        print(f"HTTP error: {e}")
        if e.response is not None:
            print("Response body:")
            print(e.response.text[:2000])
        sys.exit(3)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(4)


if __name__ == "__main__":
    main()