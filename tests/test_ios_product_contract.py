from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
IOS = ROOT / "ios" / "AssistantPocket"


def _read(relative: str) -> str:
    return (IOS / relative).read_text(encoding="utf-8")


def test_ios_uses_three_stable_native_tabs_while_today_stays_attention_first():
    root = _read("App/AppRootView.swift")
    source = _read("App/ContentView.swift")

    assert "TabView(selection: $selectedTab)" in root
    assert 'Label("Сегодня", systemImage: "house")' in root
    assert 'Label("Задачи", systemImage: "checkmark.circle")' in root
    assert 'Label("Идеи", systemImage: "lightbulb")' in root
    assert 'Text("Сейчас")' in source
    assert 'Text("Дальше")' in source
    assert 'assistant.captureDraft' in source
    assert 'Button("Все задачи")' not in source


def test_ios_does_not_embed_model_or_classifier_logic():
    swift = "\n".join(path.read_text(encoding="utf-8") for path in IOS.rglob("*.swift"))
    lowered = swift.lower()

    assert "gemini" not in lowered
    assert "deepseek" not in lowered
    assert "classify_intake" not in lowered
    assert "llm" not in lowered


def test_ideas_are_a_separate_tab_and_stay_off_today():
    root = _read("App/AppRootView.swift")
    home = _read("App/ContentView.swift")
    backlog = _read("App/AllTasksView.swift")
    ideas = _read("App/IdeasView.swift")

    assert "IdeasView" in root
    assert "IdeasView" not in home
    assert "IdeasView" not in backlog
    assert 'navigationTitle("Идеи")' in ideas
    assert "swipeActions" in ideas


def test_capture_is_one_tap_from_tasks_and_ideas_without_duplicating_intake_ui():
    tasks = _read("App/AllTasksView.swift")
    ideas = _read("App/IdeasView.swift")
    root = _read("App/AppRootView.swift")

    for source in (tasks, ideas):
        assert 'assistantpocket://capture?mode=voice' in source
        assert 'assistantpocket://capture' in source
        assert 'accessibilityLabel("Запомнить")' in source
        assert 'accessibilityLabel("Записать голосом")' in source

    assert 'selectedTab = .today' in root


def test_primary_tabs_share_one_revision_so_capture_and_mutations_do_not_leave_stale_lists():
    root = _read("App/AppRootView.swift")
    home = _read("App/ContentView.swift")
    ideas = _read("App/IdeasView.swift")

    assert "ContentView(refreshToken: taskRevision)" in root
    assert "IdeasView(refreshToken: taskRevision)" in root
    assert "let onChanged: () -> Void" in home
    assert "onChanged()" in home
    assert ".onChange(of: refreshToken)" in ideas


def test_capture_feedback_says_what_was_saved():
    home = _read("App/ContentView.swift")

    assert '"Идея сохранена"' in home
    assert '"Напоминание создано"' in home
    assert '"Встреча добавлена"' in home
    assert '"Задача записана"' in home


def test_widget_shared_keychain_does_not_hardcode_app_group_as_access_group():
    source = _read("Shared/WidgetSharedSettings.swift")

    assert "kSecAttrAccessGroup" not in source
    assert "group.0ee1e5aa54499877" not in source
    assert 'service = "com.aev199.assistantpocket.widget-shared"' in source


def test_due_reminders_take_attention_only_when_due_without_growing_today():
    home = _read("App/ContentView.swift")
    widget = _read("Widget/AssistantWidget.swift")

    assert "private var dueReminder" in home
    assert "return at <= now" in home
    assert "private var dueReminder" in widget
    assert "return at <= entry.date" in widget
    assert "focusReminder" in home
    assert "focusReminder" in widget
    assert "reservedTimed" in home
    assert "4 - reservedTimed" in home


def test_calendar_context_interrupts_without_losing_explicit_focus():
    home = _read("App/ContentView.swift")
    models = _read("App/Models.swift")
    widget = _read("Widget/AssistantWidget.swift")

    assert "TodayEvent" in models
    assert "let events: [TodayEvent]?" in models
    assert "activeEvent" in home
    assert "upcomingEvent" in home
    assert "eventFocusCard" in home
    assert 'Text("После встречи: \\(paused.title)")' in home
    assert ".onReceive(clock)" in home
    assert "WidgetEvent" in widget
    assert "focusEventView" in widget
    assert 'Text("После: \\(paused.title)")' in widget
    assert "event.start.addingTimeInterval(-15 * 60)" in widget
    assert "event.end.addingTimeInterval(5)" in widget
    assert "private var nextContent" in widget


def test_today_keeps_local_focus_action_and_tasks_page_between_work_personal():
    root = _read("App/AppRootView.swift")
    home = _read("App/ContentView.swift")
    tasks = _read("App/AllTasksView.swift")
    deadline = _read("Shared/TaskDeadlineFormatting.swift")

    assert 'Button("Изменить")' in home
    assert 'Button("Все задачи")' not in home
    assert "showAllTasks" not in home
    assert "dx < -90" not in home
    assert 'case work = "Рабочие"' in tasks
    assert 'case personal = "Личные"' in tasks
    assert 'Picker("Тип задач", selection: $scope)' in tasks
    assert "TabView(selection: $scope)" in tasks
    assert ".tabViewStyle(.page(indexDisplayMode: .never))" in tasks
    assert ".swipeActions(edge: .leading" in tasks
    assert ".swipeActions(edge: .trailing" in tasks
    assert 'Label("Сейчас", systemImage: "scope")' in tasks
    assert 'Label("Готово", systemImage: "checkmark")' in tasks
    assert 'Image(systemName: "play.fill")' not in tasks
    assert "taskDeadlineText(deadline)" in home
    assert "taskDeadlineText(deadline)" in tasks
    assert '"сегодня' in deadline
    assert '"завтра' in deadline
    assert "AllTasksView" in root


def test_calendar_attention_can_be_dismissed_without_deleting_calendar():
    home = _read("App/ContentView.swift")
    api = _read("App/APIClient.swift")
    widget = _read("Widget/AssistantWidget.swift")
    project = (ROOT / "ios" / "project.yml").read_text(encoding="utf-8")

    assert "dismissEvent(eventID: event.id, until: event.end)" in home
    assert 'accessibilityLabel("Убрать встречу из внимания")' in home
    assert "dx > 70" not in home
    assert "/api/v1/attention/dismiss-event" in api
    assert "DismissCalendarEventIntent" in widget
    assert "xmark.circle.fill" in widget
    assert "cacheWithoutEvent" in widget
    assert "taskDeadlineText(deadline)" in widget
    assert "Shared/TaskDeadlineFormatting.swift" in project


def test_now_can_be_changed_and_reminders_can_be_snoozed():
    home = _read("App/ContentView.swift")
    focus_picker = _read("App/FocusPickerView.swift")
    api = _read("App/APIClient.swift")
    widget = _read("Widget/AssistantWidget.swift")

    assert "FocusPickerView" in home
    assert "manualFocusTask" in home
    assert "tasks.prefix(5)" not in home
    assert 'navigationTitle("Что сейчас?")' in focus_picker
    assert "client.loadTasks()" in focus_picker
    assert ".searchable(" in focus_picker
    assert "focusTask(taskID: task.id)" in focus_picker
    assert "clearFocus(taskID: task.id)" in home
    assert "clearFocus(taskID: taskID)" in focus_picker
    assert "/api/v1/tasks/\(taskID)/unfocus" in api
    assert "snoozeReminder(reminderID: reminder.id, minutes: 15)" in home
    assert "/api/v1/reminders/\\(reminderID)/snooze" in api

    assert "SnoozeReminderIntent" in widget
    assert "cacheWithoutReminder" in widget
    assert "manualFocusTask" in widget
    assert "focused == true" in widget
    assert 'Text("+15")' in widget


def test_empty_now_offers_one_low_friction_start_without_auto_focusing_future_work():
    home = _read("App/ContentView.swift")

    assert "private var suggestedFocusTask" in home
    assert "deadline < startOfTomorrow" in home
    assert 'Text("Начать: \\(suggestedFocusTask.title)")' in home
    assert 'Button("Выбрать другую")' in home
    assert "client.focusTask(taskID: task.id)" in home


def test_explicit_focus_exposes_when_it_started_without_adding_a_timer_mode():
    models = _read("App/Models.swift")
    home = _read("App/ContentView.swift")
    widget = _read("Widget/AssistantWidget.swift")

    assert 'case focusedSince = "focused_since"' in models
    assert "task.focusedSince" in home
    assert 'Text("с")' in home
    assert 'case focusedSince = "focused_since"' in widget
    assert "task.focusedSince" in widget
    assert "focusedSince = Date()" in widget


def test_now_requires_explicit_focus_instead_of_promoting_first_task_implicitly():
    home = _read("App/ContentView.swift")
    widget = _read("Widget/AssistantWidget.swift")

    assert "return focusEvent == nil && focusReminder == nil ? tasks.first : nil" not in home
    assert "return focusEvent == nil && focusReminder == nil ? entry.tasks.first : nil" not in widget
    assert 'Text(tasks.isEmpty ? "На сейчас ничего нет" : "Ничего не выбрано")' in home
    assert 'Text("Ничего не выбрано")' in widget


def test_widget_can_promote_visible_next_task_to_now():
    widget = _read("Widget/AssistantWidget.swift")

    assert "FocusTaskIntent" in widget
    assert "WidgetNetwork.focusTask" in widget
    assert "cacheFocusedTask" in widget
    assert 'Text("Сейчас")' in widget
    assert 'accessibilityLabel("Сейчас")' in widget


def test_reminder_delivery_and_widget_state_do_not_compete():
    widget = _read("Widget/AssistantWidget.swift")

    assert "nextRefreshDate(for entry:" in widget
    assert "candidates.append(at)" in widget
    assert "now.addingTimeInterval(60)" in widget
    assert "event.end.addingTimeInterval(5)" in widget


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
    assert "/api/v1/companion/intake/audio" in api
    assert "Сервер Assistant не обновлён" in api
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
