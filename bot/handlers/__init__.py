"""Telegram handlers package.

Handlers are registered via explicit `register(dp)` calls to avoid cyclic imports
and to make the registration order predictable.
"""

from .nav import register as register_nav
from .projects import register as register_projects
from .tasks import register as register_tasks
from .bulk import register as register_bulk
from .wizards import register as register_wizards
from .events import register as register_events
from .team import register as register_team
from .reminders import register as register_reminders
from .system import register as register_system
from .inbox import register as register_inbox
from .errors import register as register_errors

__all__ = [
    "register_nav",
    "register_projects",
    "register_tasks",
    "register_bulk",
    "register_wizards",
    "register_events",
    "register_team",
    "register_reminders",
    "register_system",
    "register_inbox",
    "register_errors",
]
