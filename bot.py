"""Thin entrypoint.

All bot logic (handlers, services, adapters) lives under the :mod:`bot` package.
This file only starts the aiohttp webhook server.
"""

from bot.runtime import main


if __name__ == "__main__":
    main()
