"""Interactive helper to mint a Google OAuth refresh token for Google Tasks.

Usage:
    python scripts/get_google_refresh_token.py

Required environment variables:
    GOOGLE_CLIENT_ID
    GOOGLE_CLIENT_SECRET

Optional environment variables:
    GOOGLE_OAUTH_SCOPE        Default: https://www.googleapis.com/auth/tasks
    GOOGLE_OAUTH_PORT         Default: 8765
    GOOGLE_OAUTH_REDIRECT_URI Override localhost callback entirely
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer


AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
DEFAULT_SCOPE = "https://www.googleapis.com/auth/tasks"


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    server_version = "GoogleOAuthHelper/1.0"

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        self.server.auth_code = query.get("code", [None])[0]  # type: ignore[attr-defined]
        self.server.auth_error = query.get("error", [None])[0]  # type: ignore[attr-defined]

        if self.server.auth_code:  # type: ignore[attr-defined]
            body = (
                "<html><body><h2>Authorization received</h2>"
                "<p>You can return to the terminal.</p></body></html>"
            )
            self.send_response(200)
        else:
            body = (
                "<html><body><h2>Authorization failed</h2>"
                "<p>Check the terminal for details.</p></body></html>"
            )
            self.send_response(400)

        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body.encode("utf-8"))))
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


def _env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def _build_redirect_uri(port: int) -> str:
    override = os.getenv("GOOGLE_OAUTH_REDIRECT_URI", "").strip()
    if override:
        return override
    return f"http://127.0.0.1:{port}/callback"


def _start_callback_server(port: int) -> tuple[HTTPServer, threading.Thread]:
    httpd = HTTPServer(("127.0.0.1", port), OAuthCallbackHandler)
    httpd.auth_code = None  # type: ignore[attr-defined]
    httpd.auth_error = None  # type: ignore[attr-defined]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, thread


def _authorization_url(*, client_id: str, redirect_uri: str, scope: str, state: str) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scope,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


def _exchange_code(*, client_id: str, client_secret: str, redirect_uri: str, code: str) -> dict:
    payload = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        TOKEN_URL,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Token exchange failed: HTTP {exc.code}: {body}") from exc


def main() -> None:
    client_id = _env("GOOGLE_CLIENT_ID")
    client_secret = _env("GOOGLE_CLIENT_SECRET")
    scope = os.getenv("GOOGLE_OAUTH_SCOPE", DEFAULT_SCOPE).strip() or DEFAULT_SCOPE
    port = int(os.getenv("GOOGLE_OAUTH_PORT", "8765"))
    redirect_uri = _build_redirect_uri(port)
    state = secrets.token_urlsafe(24)

    print("Google OAuth refresh token helper")
    print(f"Scope: {scope}")
    print(f"Redirect URI: {redirect_uri}")
    print()
    print("Important:")
    print("1. This redirect URI must be allowed in your Google OAuth client.")
    print("2. If your OAuth app is of type Web application, add the URI in Authorized redirect URIs.")
    print("3. If you use another redirect URI, set GOOGLE_OAUTH_REDIRECT_URI.")
    print()

    httpd = None
    if redirect_uri.startswith("http://127.0.0.1:"):
        httpd, _thread = _start_callback_server(port)

    auth_url = _authorization_url(
        client_id=client_id,
        redirect_uri=redirect_uri,
        scope=scope,
        state=state,
    )

    print("Open this URL if the browser does not start automatically:")
    print(auth_url)
    print()
    webbrowser.open(auth_url, new=1)

    code = None
    error = None
    if httpd is not None:
        deadline = time.time() + 300
        while time.time() < deadline:
            code = httpd.auth_code  # type: ignore[attr-defined]
            error = httpd.auth_error  # type: ignore[attr-defined]
            if code or error:
                break
            time.sleep(0.2)
        httpd.shutdown()
        httpd.server_close()

    if error:
        raise SystemExit(f"Authorization failed: {error}")

    if not code:
        print("No authorization code received automatically.")
        print("If Google redirected to your browser, copy the `code` query parameter and paste it below.")
        code = input("Authorization code: ").strip()
        if not code:
            raise SystemExit("Authorization code is required")

    tokens = _exchange_code(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        code=code,
    )

    refresh_token = str(tokens.get("refresh_token") or "").strip()
    if not refresh_token:
        print(json.dumps(tokens, ensure_ascii=False, indent=2))
        raise SystemExit(
            "Google did not return a refresh_token. "
            "Try again with prompt=consent and ensure you use the same OAuth client."
        )

    print()
    print("Success. New refresh token:")
    print(refresh_token)
    print()
    print("Set this in your environment:")
    print(f"GOOGLE_REFRESH_TOKEN={refresh_token}")


if __name__ == "__main__":
    main()
