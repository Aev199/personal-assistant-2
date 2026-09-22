from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
IOS = ROOT / "ios" / "AssistantPocket"


def _read(relative: str) -> str:
    return (IOS / relative).read_text(encoding="utf-8")


def test_ios_home_stays_attention_first_without_tab_bar():
    source = _read("App/ContentView.swift")

    assert 'Text("Сейчас")' in source
    assert 'Text("Дальше")' in source
    assert 'assistant.captureDraft' in source
    assert "TabView" not in source


def test_ios_does_not_embed_model_or_classifier_logic():
    swift = "\n".join(path.read_text(encoding="utf-8") for path in IOS.rglob("*.swift"))
    lowered = swift.lower()

    assert "gemini" not in lowered
    assert "deepseek" not in lowered
    assert "classify_intake" not in lowered
    assert "llm" not in lowered


def test_ideas_stay_off_the_default_attention_surface():
    home = _read("App/ContentView.swift")
    backlog = _read("App/AllTasksView.swift")
    ideas = _read("App/IdeasView.swift")

    assert "IdeasView" not in home
    assert "IdeasView" in backlog
    assert 'navigationTitle("Идеи")' in ideas
    assert "swipeActions" in ideas
    assert "TabView" not in ideas


def test_widget_shared_keychain_does_not_hardcode_app_group_as_access_group():
    source = _read("Shared/WidgetSharedSettings.swift")

    assert "kSecAttrAccessGroup" not in source
    assert "group.0ee1e5aa54499877" not in source
    assert 'service = "com.aev199.assistantpocket.widget-shared"' in source


def test_due_reminders_can_take_attention_without_growing_today():
    home = _read("App/ContentView.swift")
    widget = _read("Widget/AssistantWidget.swift")

    assert "15 * 60" in home
    assert "15 * 60" in widget
    assert "focusReminder" in home
    assert "focusReminder" in widget
    assert "reservedTimed" in home
    assert "4 - reservedTimed" in home


def test_calendar_context_stays_attention_first():
    home = _read("App/ContentView.swift")
    models = _read("App/Models.swift")
    widget = _read("Widget/AssistantWidget.swift")

    assert "TodayEvent" in models
    assert "let events: [TodayEvent]?" in models
    assert "activeEvent" in home
    assert "upcomingEvent" in home
    assert "eventFocusCard" in home
    assert "WidgetEvent" in widget
    assert "focusEventView" in widget
    assert "15 * 60" in widget


def test_today_uses_swipe_for_backlog_and_shows_relative_task_dates():
    home = _read("App/ContentView.swift")
    tasks = _read("App/AllTasksView.swift")
    deadline = _read("Shared/TaskDeadlineFormatting.swift")

    assert 'Image(systemName: "list.bullet")' not in home
    assert "DragGesture(minimumDistance: 24)" in home
    assert "dx < -70" in home
    assert "navigationDestination(isPresented: $showAllTasks)" in home
    assert "taskDeadlineText(deadline)" in home
    assert "taskDeadlineText(deadline)" in tasks
    assert '"сегодня' in deadline
    assert '"завтра' in deadline


def test_calendar_attention_can_be_dismissed_without_deleting_calendar():
    home = _read("App/ContentView.swift")
    api = _read("App/APIClient.swift")
    widget = _read("Widget/AssistantWidget.swift")
    project = (ROOT / "ios" / "project.yml").read_text(encoding="utf-8")

    assert "dismissEvent(eventID: event.id, until: event.end)" in home
    assert "dx > 70" in home
    assert "/api/v1/attention/dismiss-event" in api
    assert "DismissCalendarEventIntent" in widget
    assert "xmark.circle.fill" in widget
    assert "cacheWithoutEvent" in widget
    assert "taskDeadlineText(deadline)" in widget
    assert "Shared/TaskDeadlineFormatting.swift" in project


def test_now_can_be_changed_and_reminders_can_be_snoozed():
    home = _read("App/ContentView.swift")
    api = _read("App/APIClient.swift")
    widget = _read("Widget/AssistantWidget.swift")

    assert 'confirmationDialog(' in home
    assert '"Что сейчас?"' in home
    assert "manualFocusTask" in home
    assert "focusTask(taskID: task.id)" in home
    assert "snoozeReminder(reminderID: reminder.id, minutes: 15)" in home
    assert "/api/v1/reminders/\\(reminderID)/snooze" in api

    assert "SnoozeReminderIntent" in widget
    assert "cacheWithoutReminder" in widget
    assert "manualFocusTask" in widget
    assert "focused == true" in widget
    assert 'Text("+15")' in widget


def test_reminder_delivery_and_widget_state_do_not_compete():
    widget = _read("Widget/AssistantWidget.swift")

    assert "nextRefreshDate(for entry:" in widget
    assert "attentionStart = at.addingTimeInterval(-15 * 60)" in widget
    assert "at.addingTimeInterval(5)" in widget
    assert "now.addingTimeInterval(60)" in widget


def test_voice_capture_is_fast_shared_and_loss_resistant():
    home = _read("App/ContentView.swift")
    api = _read("App/APIClient.swift")
    widget = _read("Widget/AssistantWidget.swift")
    recorder = _read("App/VoiceRecorder.swift")
    outbox = _read("App/VoiceCaptureOutbox.swift")
    project = (ROOT / "ios" / "project.yml").read_text(encoding="utf-8")

    assert '"mic.fill"' in home
    assert "voiceIntake(" in home
    assert "VoiceCaptureOutbox.enqueue" in home
    assert "flushVoiceOutbox()" in home
    assert 'mode == "voice"' in home

    assert "/api/v1/intake/audio" in api
    assert "multipart/form-data" in api
    assert "assistantpocket://capture?mode=voice" in widget
    assert '"mic.circle.fill"' in widget

    assert "AVAudioRecorder" in recorder
    assert "AVAudioApplication.requestRecordPermission" in recorder
    assert "applicationSupportDirectory" in outbox
    assert "NSMicrophoneUsageDescription" in project


def test_widget_keeps_esign_safe_static_configuration():
    source = _read("Widget/AssistantWidget.swift")

    assert "StaticConfiguration" in source
    assert "AppIntentConfiguration" not in source
    assert "WidgetSharedSettings" in source
    assert "reminders: [],\n                error:" not in source


def test_capture_is_loss_resistant():
    content = _read("App/ContentView.swift")
    outbox = _read("App/CaptureOutbox.swift")

    assert "CaptureOutbox.enqueue" in content
    assert "flushOutbox()" in content
    assert "UserDefaults.standard" in outbox
