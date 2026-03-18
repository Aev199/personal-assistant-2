"""SPA UI layer.

- state: persistence for the single-screen UI (ui_message_id/ui_screen/ui_payload)
- render: low-level SPA renderer (edit or send + persist state)
- screens: high-level screen renderers (Home/Projects/Today/Overdue/Help/Add)
"""

from .state import ui_get_state, ui_set_state
from .render import ui_render
from .screens import (
    ui_render_home,
    ui_render_home_more,
    ui_render_stats,
    ui_render_help,
    ui_render_add_menu,
    ui_render_all_tasks,
    ui_render_projects_portfolio,
    ui_render_reminders,
    ui_render_today,
    ui_render_overdue,
    ui_render_work,
    ui_render_inbox,
    ui_render_team,
    cleanup_main_menu_anchor,
    ensure_main_menu,
)

__all__ = [
    "ui_get_state",
    "ui_set_state",
    "ui_render",
    "ui_render_home",
    "ui_render_home_more",
    "ui_render_stats",
    "ui_render_help",
    "ui_render_add_menu",
    "ui_render_all_tasks",
    "ui_render_projects_portfolio",
    "ui_render_reminders",
    "ui_render_today",
    "ui_render_overdue",
    "ui_render_work",
    "ui_render_inbox",
    "ui_render_team",
    "cleanup_main_menu_anchor",
    "ensure_main_menu",
]
