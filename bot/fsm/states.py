"""FSM state groups.

Kept in a dedicated module so handlers can share states without importing the monolith.
"""

from aiogram.fsm.state import State, StatesGroup


class AddTaskWizard(StatesGroup):
    """Wizard for task/subtask creation."""

    choosing_project = State()
    choosing_parent = State()
    choosing_assignee = State()
    entering_title = State()
    choosing_deadline = State()
    entering_deadline = State()
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


class QuickIdeaWizard(StatesGroup):
    """Quick capture for personal ideas into Google Tasks."""

    entering_text = State()


class AddTeamWizard(StatesGroup):
    entering = State()


class EditTeamNoteWizard(StatesGroup):
    entering = State()


class EditTaskDeadline(StatesGroup):
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


class AddSuperTaskWizard(StatesGroup):
    entering_title = State()
    confirming = State()


class FreeformFollowup(StatesGroup):
    """Short follow-up after free-form intake for clarifications."""

    awaiting_text = State()


class InitialSetup(StatesGroup):
    """One-shot brain-dump onboarding flow."""

    awaiting_dump = State()
