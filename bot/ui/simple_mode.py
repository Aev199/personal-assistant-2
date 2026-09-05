"""Install the minimal daily UI before handlers bind screen functions."""

from __future__ import annotations

_installed = False


def install_simple_ui() -> None:
    global _installed
    if _installed:
        return

    import bot.ui as ui_package
    import bot.ui.screens as screens
    from bot.ui import simple_daily

    replacements = {
        "ui_render_home": simple_daily.ui_render_home,
        "ui_render_home_more": simple_daily.ui_render_home_more,
        "ui_render_help": simple_daily.ui_render_help,
        "ui_render_add_menu": simple_daily.ui_render_add_menu,
        "ui_render_all_tasks": simple_daily.ui_render_all_tasks,
        "ui_render_today": simple_daily.ui_render_today,
    }
    for name, func in replacements.items():
        setattr(screens, name, func)
        setattr(ui_package, name, func)

    _installed = True
