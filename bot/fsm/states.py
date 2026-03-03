"""FSM state groups.

Kept in a dedicated module so handlers can share states without importing the monolith.
"""

from aiogram.fsm.state import State, StatesGroup


class AddTaskWizard(StatesGroup):
    """Wizard for task/subtask creation.

    Mode is stored in FSM data: wizard_mode = 'task' | 'subtask'
    """

    choosing_project = State()
    choosing_parent = State()      # only for subtask mode
    choosing_assignee = State()
    entering_title = State()
    choosing_deadline = State()
    # Manual deadline input
    entering_deadline = State()
    # Backward-compatible alias (some modules used `entering`)
    entering = State()
    confirming = State()


class AddProjectWizard(StatesGroup):
    entering_data = State()


class AddEventWizard(StatesGroup):
    """Wizard for creating iCloud CalDAV events (create-only)."""

    choosing_kind = State()
    entering_title = State()
    choosing_date = State()
    entering_date = State()
    choosing_time = State()
    entering_time = State()
    choosing_duration = State()
    entering_duration = State()
    confirming = State()


class QuickTaskWizard(StatesGroup):
    """Quick capture for work tasks into INBOX project."""

    entering_text = State()


class QuickIdeaWizard(StatesGroup):
    """Quick capture for personal ideas into Google Tasks."""

    entering_text = State()


class AddTeamWizard(StatesGroup):
    """Add team member from SPA UI."""

    entering = State()


class EditTaskDeadline(StatesGroup):
    """Inline deadline editor (used from task card)."""

    entering = State()


class AddReminderWizard(StatesGroup):
    choosing_time = State()
    entering_time = State()
    entering_text = State()
    choosing_repeat = State()


class AddPersonalWizard(StatesGroup):
    entering_text = State()
    choosing_deadline = State()
    entering_deadline = State()
    entering = State()
