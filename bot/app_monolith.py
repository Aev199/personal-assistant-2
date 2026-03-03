"""Legacy monolith (compatibility stub).

The project originally shipped as a single large monolithic module.
The refactor moved production runtime to :mod:`bot.runtime` + modular handlers.

Why keep this file?
-------------------
Some old scripts / snippets may still do ``import bot.app_monolith``.
To avoid breaking those imports, this stub optionally re-exports the legacy
module when explicitly enabled.

The legacy source is kept outside the :mod:`bot` package under ``legacy_src/``
so it cannot be imported accidentally in production.

Usage
-----
Set ``ENABLE_LEGACY_MONOLITH=1`` in environment if you really need the legacy
symbols.
"""

from __future__ import annotations

import os
import warnings


if os.getenv("ENABLE_LEGACY_MONOLITH", "").strip() in {"1", "true", "True", "yes", "YES"}:
    warnings.warn(
        "ENABLE_LEGACY_MONOLITH is set: importing legacy_src.app_monolith. "
        "Prefer bot.runtime/create_app + bot.handlers.* for production.",
        RuntimeWarning,
        stacklevel=2,
    )
    from legacy_src.app_monolith import *  # noqa: F401,F403
else:
    # Intentionally empty. Production code should not rely on the monolith.
    __all__: list[str] = []
