"""Very small smoke test.

This is meant for CI/Render troubleshooting: it validates that the project
imports, that aiohttp app can be constructed, and that the expected routes are
present.

Run:
    python -m scripts.smoke
"""

from __future__ import annotations

from aiohttp import web


def _route_paths(app: web.Application) -> set[str]:
    paths: set[str] = set()
    for r in app.router.routes():
        try:
            info = r.get_info()
            p = info.get("path")
            if p:
                paths.add(p)
        except Exception:
            continue
    return paths


def main() -> None:
    # Provide minimal env defaults so this can run in fresh CI containers.
    import os

    os.environ.setdefault("BOT_TOKEN", "000:smoke")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/db")
    os.environ.setdefault("WEBHOOK_URL", "https://example.com")
    os.environ.setdefault("WEBHOOK_PATH", "/webhook")

    from bot.runtime import create_app_webhook

    app = create_app_webhook()
    paths = _route_paths(app)

    required = {"/ping", "/health", "/tick", "/backup"}
    missing = required - paths
    if missing:
        raise SystemExit(f"Smoke failed: missing routes: {sorted(missing)}")

    print("Smoke OK. Routes:", ", ".join(sorted(required)))


if __name__ == "__main__":
    main()
