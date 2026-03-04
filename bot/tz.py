"""Timezone resolution helpers.

Why this exists:
- Hosting providers (Render/Heroku/etc.) often set TZ=UTC at the process level.
- In this project, deadlines/reminders are stored as *naive UTC* timestamps in DB.
  For correct UX, we must always know the *application* timezone used for UI.

We therefore prefer an explicit app timezone env var and only fall back to TZ
when it looks intentionally set by the user.
"""

from __future__ import annotations

import os


def resolve_tz_name(default: str = "Europe/Moscow") -> str:
    """Resolve application timezone name.

    Priority:
      1) BOT_TIMEZONE / APP_TIMEZONE / BOT_TZ
      2) TZ (only if it doesn't look like a provider default like UTC)
      3) default
    """

    for k in ("BOT_TIMEZONE", "APP_TIMEZONE", "BOT_TZ"):
        v = (os.getenv(k) or "").strip()
        if v:
            return v

    tz = (os.getenv("TZ") or "").strip()
    if tz and tz.upper() not in {"UTC", "ETC/UTC", "GMT", "ETC/GMT"}:
        return tz

    return default
