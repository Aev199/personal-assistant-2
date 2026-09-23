from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
IOS = ROOT / "ios" / "AssistantPocket"


def _read(relative: str) -> str:
    return (IOS / relative).read_text(encoding="utf-8")


def test_ios_product_docs_match_current_tabs_voice_and_attention_semantics():
    readme = (ROOT / "ios" / "README.md").read_text(encoding="utf-8")
    product = (ROOT / "ios" / "PRODUCT.md").read_text(encoding="utf-8")

    assert "three stable native tabs" in readme
    assert "No tab bar" not in readme
    assert "on-device SpeechAnalyzer transcription first" in readme
    assert "transcribed on-device with SpeechAnalyzer" in product
    assert "A reminder takes **Сейчас** only when it is due" in product
    assert "explicit task remains remembered underneath" in product


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


def test_widget_mutations_use_a_non_reentrant_gate_to_avoid_cache_races():
    widget = _read("Widget/AssistantWidget.swift")

    assert "private actor WidgetMutationCoordinator" in widget
    assert "private var isRunning = false" in widget
    assert "private var waiters: [CheckedContinuation<Void, Never>] = []" in widget
    assert "await acquire()" in widget
    assert "defer { release() }" in widget
    assert "async throws -> Void" in widget
    assert "func run<T: Sendable>" not in widget
    assert "try Task.checkCancellation()" in widget

    for intent in (
        "MarkTaskDoneIntent",
        "FocusTaskIntent",
        "ClearFocusTaskIntent",
        "DismissCalendarEventIntent",
        "AcknowledgeReminderIntent",
        "SnoozeReminderIntent",
    ):
        block = widget[widget.index(f"struct {intent}:"):]
        block = block[:block.index("\n}\n", block.index("func perform")) + 3]
        assert "WidgetMutationCoordinator.shared.run" in block


def test_widget_clear_focus_updates_cached_state_optimistically_and_rolls_back():
    widget = _read("Widget/AssistantWidget.swift")

    assert "static func cacheWithoutFocus(_ taskID: Int)" in widget
    block = widget[widget.index("struct ClearFocusTaskIntent"):]
    assert "WidgetCodec.cacheWithoutFocus(taskID)" in block
    assert "WidgetSharedSettings.writeCachedTodayData(originalCache)" in block
    assert "WidgetSharedSettings.clearCachedTodayData()" in block


def test_app_refreshes_primary_data_when_returning_to_foreground():
    root = _read("App/AppRootView.swift")

    assert '@Environment(\\.scenePhase) private var scenePhase' in root
    assert '.onChange(of: scenePhase)' in root
    assert 'guard phase == .active else { return }' in root
    assert 'taskRevision += 1' in root
    assert 'WidgetCenter.shared.reloadTimelines(ofKind: "AssistantPocketWidget")' in root


def test_explicit_now_always_wins_over_calendar_attention_in_app():
    home = _read("App/ContentView.swift")

    focus_event = home[home.index("private var focusEvent:"):home.index("private var focusReminder:")]
    focus_reminder = home[home.index("private var focusReminder:"):home.index("private var focusTask:")]

    assert 'guard manualFocusTask == nil else { return nil }' in focus_event
    assert 'guard manualFocusTask == nil, activeEvent == nil else { return nil }' in focus_reminder
    assert 'Text("↩︎ вернуться")' not in home


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


def test_task_page_swipe_does_not_compete_with_row_action_swipes():
    tasks = _read("App/AllTasksView.swift")

    assert ".tabViewStyle(.page(indexDisplayMode: .never))" in tasks
    assert ".swipeActions(" not in tasks
    assert ".contextMenu" in tasks
    assert 'Image(systemName: "circle")' in tasks


def test_promoting_idea_reconciles_widget_attention_state():
    ideas = _read("App/IdeasView.swift")

    assert "import WidgetKit" in ideas
    assert 'WidgetCenter.shared.reloadTimelines(ofKind: "AssistantPocketWidget")' in ideas


def test_ideas_keep_actions_without_permanent_instructional_noise():
    ideas = _read("App/IdeasView.swift")

    assert ".swipeActions(" in ideas
    assert ".contextMenu" in ideas
    assert "Смахните идею:" not in ideas


def test_capture_deep_links_are_routed_at_root_after_switching_to_today():
    root = _read("App/AppRootView.swift")
    home = _read("App/ContentView.swift")
    intents = _read("Shared/CaptureIntents.swift")

    assert 'selectedTab = .today' in root
    assert 'mode == "voice" ? .voice : .text' in root
    assert "CaptureLaunchSignal.request(mode:" in root
    assert ".onOpenURL" not in home
    assert "CaptureLaunchSignal.mode(from: notification)" in home
    assert "CaptureLaunchSignal.consumeMode()" in home
    assert "enum CaptureLaunchMode" in intents


def test_external_quick_capture_cannot_be_hijacked_by_old_clarification_context():
    home = _read("App/ContentView.swift")

    start = home.index("private func activateCapture(mode:")
    end = home.index("private func activateVoiceCapture", start)
    block = home[start:end]

    assert "clearClarification()" in block
    assert block.index("clearClarification()") < block.index("activateVoiceCapture()")


def test_cold_start_capture_runs_before_refresh_and_outbox_work():
    home = _read("App/ContentView.swift")

    task_start = home.index(".task {")
    task_end = home.index(".onReceive(", task_start)
    task_block = home[task_start:task_end]

    assert task_block.index("consumeSystemCaptureRequest()") < task_block.index("await loadToday()")
    assert task_block.index("consumeSystemCaptureRequest()") < task_block.index("await flushOutbox()")


def test_capture_mode_is_durable_across_cold_start_or_late_subscription():
    shared = _read("Shared/WidgetSharedSettings.swift")
    intents = _read("Shared/CaptureIntents.swift")
    home = _read("App/ContentView.swift")

    assert "requestCaptureLaunch(mode:" in shared
    assert "consumeCaptureLaunchMode()" in shared
    assert 'return raw == "1" ? "text" : raw' in shared
    assert "static func consumeMode()" in intents
    assert "guard let mode = CaptureLaunchSignal.consumeMode()" in home
    assert "activateCapture(mode: mode)" in home


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


def test_tasks_and_ideas_mark_retained_data_as_stale_after_refresh_failure():
    tasks = _read("App/AllTasksView.swift")
    ideas = _read("App/IdeasView.swift")

    for source in (tasks, ideas):
        assert "@State private var dataStale = false" in source
        assert 'Label("Показаны последние данные", systemImage: "wifi.slash")' in source
        assert "dataStale = false" in source

    assert "dataStale = !tasks.isEmpty" in tasks
    assert "dataStale = !ideas.isEmpty" in ideas


def test_overlapping_refreshes_cannot_overwrite_newer_primary_tab_state():
    home = _read("App/ContentView.swift")
    tasks = _read("App/AllTasksView.swift")
    ideas = _read("App/IdeasView.swift")

    assert "todayLoadGeneration += 1" in home
    assert "guard generation == todayLoadGeneration else { return }" in home
    assert "loadGeneration += 1" in tasks
    assert "guard generation == loadGeneration else { return }" in tasks
    assert "loadGeneration += 1" in ideas
    assert "guard generation == loadGeneration else { return }" in ideas


def test_primary_tabs_share_one_revision_so_capture_and_mutations_do_not_leave_stale_lists():
    root = _read("App/AppRootView.swift")
    home = _read("App/ContentView.swift")
    ideas = _read("App/IdeasView.swift")

    assert "ContentView(refreshToken: taskRevision)" in root
    assert "IdeasView(refreshToken: taskRevision)" in root
    assert "let onChanged: () -> Void" in home
    assert "onChanged()" in home
    assert ".onChange(of: refreshToken)" in ideas


def test_today_keeps_last_good_state_but_marks_it_stale_after_refresh_failure():
    home = _read("App/ContentView.swift")

    assert "@State private var todayStale = false" in home
    assert "private var dataFreshnessWarning" in home
    assert '"Не удалось обновить · показаны последние данные"' in home
    assert "if hasLoadedToday" in home
    assert "todayStale = true" in home
    assert "todayStale = false" in home


def test_today_does_not_misreport_empty_day_when_first_load_failed():
    home = _read("App/ContentView.swift")

    assert "@State private var hasLoadedToday = false" in home
    assert '"Не удалось обновить"' in home
    assert '"Проверьте соединение или VPN.' in home
    assert 'Button("Повторить")' in home
    assert "hasLoadedToday = true" in home
    assert "if hasLoadedToday, let errorMessage" in home


def test_capture_feedback_is_ephemeral_instead_of_becoming_screen_clutter():
    home = _read("App/ContentView.swift")

    assert "private func presentConfirmation" in home
    assert "confirmationRevision" in home
    assert ".now() + 2.4" in home


def test_capture_feedback_says_what_was_saved():
    home = _read("App/ContentView.swift")

    assert '"Идея сохранена"' in home
    assert '"Напоминание создано"' in home
    assert '"Встреча добавлена"' in home
    assert '"Задача записана"' in home


def test_connection_settings_require_an_http_url_not_just_any_url_string():
    settings = _read("App/AppSettings.swift")
    home = _read("App/ContentView.swift")

    assert "static func isValidBaseURL" in settings
    assert 'scheme == "http" || scheme == "https"' in settings
    assert "url.host != nil" in settings
    assert "AppSettings.isValidBaseURL(normalizedDraftURL)" in home


def test_editing_task_does_not_resubmit_unchanged_project_code():
    editor = _read("App/TaskEditView.swift")

    assert "let originalProjectCode" in editor
    assert "projectCode.caseInsensitiveCompare(originalProjectCode)" in editor
    assert "projectCode: projectChanged ? projectCode : nil" in editor


def test_editing_task_never_silently_moves_unknown_project_to_inbox():
    editor = _read("App/TaskEditView.swift")

    assert '"Текущий · \\(projectCode)"' in editor
    assert ".tag(projectCode)" in editor
    assert "projectCode = inbox.code" not in editor


def test_settings_edit_does_not_break_saved_connection_until_user_saves():
    home = _read("App/ContentView.swift")

    assert "@State private var baseURLDraft" in home
    assert "@State private var tokenDraft" in home
    assert 'TextField("Адрес Assistant", text: $baseURLDraft)' in home
    assert 'SecureField("Код доступа", text: $tokenDraft)' in home
    assert 'Button("Сохранить")' in home
    assert 'Button("Отмена")' in home
    assert ".interactiveDismissDisabled(!settings.isConfigured)" in home
    assert 'TextField("Адрес Assistant", text: $settings.baseURL)' not in home


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


def test_timed_items_in_next_are_ordered_by_clock_time_in_app_and_widget():
    home = _read("App/ContentView.swift")
    widget = _read("Widget/AssistantWidget.swift")

    assert "private var nextTimedRows" in home
    assert "reminderAt <= event.start" in home
    assert "private var nextTimedContent" in widget
    assert "reminderAt <= event.start" in widget


def test_slow_calendar_gets_one_fast_followup_instead_of_looking_empty():
    models = _read("App/Models.swift")
    home = _read("App/ContentView.swift")
    widget = _read("Widget/AssistantWidget.swift")

    assert 'case calendarPending = "calendar_pending"' in models
    assert "@State private var calendarPending = false" in home
    assert '"Календарь загружается…"' in home
    assert ".now() + 1.5" in home
    assert "didRetryPendingCalendar" in home
    assert 'case calendarPending = "calendar_pending"' in widget
    assert "entry.error != nil || entry.calendarPending" in widget
    assert 'accessibilityLabel("Календарь загружается")' in widget


def test_calendar_failure_is_visible_instead_of_looking_like_a_free_day():
    home = _read("App/ContentView.swift")
    widget = _read("Widget/AssistantWidget.swift")

    assert "@State private var calendarUnavailable = false" in home
    assert 'Label("Календарь не обновился"' in home
    assert "response.calendarUnavailable ?? false" in home
    assert 'case calendarUnavailable = "calendar_unavailable"' in widget
    assert "entry.calendarUnavailable" in widget
    assert 'accessibilityLabel("Календарь не обновился")' in widget


def test_widget_explicit_focus_has_same_priority_and_clear_action_as_app():
    widget = _read("Widget/AssistantWidget.swift")

    assert "static func clearFocus(taskID: Int) async throws" in widget
    assert "struct ClearFocusTaskIntent: AppIntent" in widget
    assert "guard manualFocusTask == nil else { return nil }" in widget
    assert 'Text("Не сейчас")' in widget
    assert 'Text("вернуться")' not in widget


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
    assert ".swipeActions(" not in tasks
    assert ".contextMenu" in tasks
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


def test_stale_pending_confirmation_refreshes_instead_of_leaving_dead_card():
    home = _read("App/ContentView.swift")

    confirm_start = home.index("private func confirmPending")
    reminder_start = home.index("private func acknowledge", confirm_start)
    block = home[confirm_start:reminder_start]

    assert block.count("APIClientError.http(code, _) where code == 409") == 2
    assert block.count("await loadPendingIntake()") == 2
    assert block.count('"Эта запись уже обработана"') == 2


def test_voice_and_pending_action_failures_use_immediate_alert_surface():
    home = _read("App/ContentView.swift")

    assert home.count("presentActionError(error)") >= 10
    start = home.index("private func startVoiceCapture")
    end = home.index("private func flushVoiceOutbox", start)
    voice_block = home[start:end]
    assert "present(error)" not in voice_block


def test_action_failures_are_visible_on_attention_surfaces():
    home = _read("App/ContentView.swift")
    focus_picker = _read("App/FocusPickerView.swift")

    assert 'alert("Не удалось выполнить действие"' in home
    assert "presentActionError(error)" in home
    assert 'alert("Не удалось изменить «Сейчас»"' in focus_picker
    assert "errorMessage != nil && !tasks.isEmpty" in focus_picker


def test_due_reminder_can_be_acknowledged_in_app_or_widget_to_stop_future_delivery():
    home = _read("App/ContentView.swift")
    api = _read("App/APIClient.swift")
    widget = _read("Widget/AssistantWidget.swift")

    assert 'Button("ОК")' in home
    assert "client.acknowledgeReminder(reminderID: reminder.id)" in home
    assert "/api/v1/reminders/\\(reminderID)/ack" in api
    assert "AcknowledgeReminderIntent" in widget
    assert "WidgetNetwork.acknowledgeReminder" in widget
    assert 'accessibilityLabel("Закрыть напоминание")' in widget


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


def test_today_foreground_recovery_does_not_duplicate_root_refresh():
    root = _read("App/AppRootView.swift")
    home = _read("App/ContentView.swift")

    assert "if phase == .active" in home
    assert "detectReturnGap()" in home
    assert "didRetryPendingCalendar = false" in home
    assert "current - lastOpenedAt >= 36 * 60 * 60" in home

    scene_start = home.index(".onChange(of: scenePhase)")
    refresh_start = home.index(".onChange(of: refreshToken)", scene_start)
    foreground_block = home[scene_start:refresh_start]
    assert "await loadToday()" not in foreground_block
    assert "await loadPendingIntake()" in foreground_block
    assert "await flushOutbox()" in foreground_block
    assert "await flushVoiceOutbox()" in foreground_block

    assert "taskRevision += 1" in root
    assert "ContentView(refreshToken: taskRevision)" in root


def test_return_after_a_long_gap_collapses_reentry_to_one_task():
    home = _read("App/ContentView.swift")

    assert '@AppStorage("assistant.lastOpenedAt")' in home
    assert "36 * 60 * 60" in home
    assert '"Вернуться к одной задаче"' in home
    assert "returningAfterBreak = false" in home


def test_returning_copy_matches_the_action_instead_of_saying_start_again():
    home = _read("App/ContentView.swift")

    assert '"Вернуться к одной задаче"' in home
    assert '"Вернуться: \\(suggestedFocusTask.title)"' in home


def test_not_now_does_not_immediately_recommend_the_same_task_again():
    home = _read("App/ContentView.swift")

    assert "@State private var justUnfocusedTaskID" in home
    assert "task.id != justUnfocusedTaskID" in home
    assert "justUnfocusedTaskID = task.id" in home
    assert "justUnfocusedTaskID = nil" in home


def test_empty_now_offers_one_low_friction_start_without_auto_focusing_future_work():
    home = _read("App/ContentView.swift")

    assert "private var suggestedFocusTask" in home
    assert "deadline < startOfTomorrow" in home
    assert '"Начать: \\(suggestedFocusTask.title)"' in home
    assert 'Button("Выбрать другую")' in home
    assert "client.focusTask(taskID: task.id)" in home


def test_start_help_stays_one_action_instead_of_becoming_a_subtask_list():
    home = _read("App/ContentView.swift")

    assert 'Button("Первый шаг")' in home
    assert "startHelpSteps.first" in home
    assert "ForEach(Array(startHelpSteps.enumerated())" not in home


def test_start_help_is_cached_locally_to_avoid_repeated_llm_calls():
    home = _read("App/ContentView.swift")

    assert "startHelpCache: [Int: [String]]" in home
    assert "if let cached = startHelpCache[task.id]" in home
    assert "startHelpCache[task.id] = response.steps" in home


def test_current_task_can_request_tiny_start_help_without_new_navigation():
    home = _read("App/ContentView.swift")
    api = _read("App/APIClient.swift")
    models = _read("App/Models.swift")

    assert 'Button("Первый шаг")' in home
    assert "startHelpSteps.first" in home
    assert 'Text("Ищу первый шаг…")' in home
    assert "client.taskStartHelp(taskID: task.id)" in home
    assert 'path: "/api/v1/tasks/\\(taskID)/steps"' in api
    assert "struct TaskStartHelpResponse" in models


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
    assert '"Ничего не выбрано"' in home
    assert '"На сейчас ничего нет"' in home
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


def test_speech_transcriber_uses_current_stable_file_transcription_preset():
    local = _read("App/LocalSpeechTranscriber.swift")

    assert "preset: .transcription" in local
    assert ".offlineTranscription" not in local


def test_personal_client_targets_modern_speech_stack_and_prewarms_it_during_recording():
    project = (ROOT / "ios" / "project.yml").read_text(encoding="utf-8")
    home = _read("App/ContentView.swift")
    local = _read("App/LocalSpeechTranscriber.swift")

    assert 'iOS: "26.0"' in project
    assert "NSSpeechRecognitionUsageDescription" in project
    assert "LocalSpeechTranscriber.preparePreferredAssets()" in home
    assert "static func preparePreferredAssets()" in local
    assert "#available(iOS 26.0" not in home
    assert "#available(iOS 26.0" not in local


def test_transcribed_voice_keeps_audio_backup_until_server_acknowledges_text():
    home = _read("App/ContentView.swift")

    make_start = home.index("private func makeLocalVoiceTextCapture")
    send_start = home.index("private func sendTranscribedVoiceCapture")
    flush_start = home.index("private func flushVoiceOutbox")
    make_block = home[make_start:send_start]
    send_block = home[send_start:flush_start]

    assert "CaptureOutbox.enqueue(" in make_block
    assert "VoiceCaptureOutbox.remove(queued.id)" not in make_block
    assert 'if response.status == "stored"' in send_block
    assert "VoiceCaptureOutbox.remove(queued.id)" in send_block
    assert "CaptureOutbox.remove(queued.id)" in send_block


def test_voice_audio_to_text_handoff_is_recoverable_after_a_crash():
    home = _read("App/ContentView.swift")
    outbox = _read("App/CaptureOutbox.swift")

    assert "static func item(id: UUID)" in outbox
    assert "CaptureOutbox.item(id: queued.id)" in home
    assert "CaptureOutbox.item(id: item.id)" in home
    assert "await sendTranscribedVoiceCapture(existingText)" in home


def test_speech_analyzer_cancellation_cannot_return_partial_transcript():
    local = _read("App/LocalSpeechTranscriber.swift")

    assert local.count("let lastSample = try await analyzer.analyzeSequence(from: audioFile)") == 2
    assert local.count("if Task.isCancelled") == 2
    assert local.count("await analyzer.cancelAndFinishNow()") >= 4
    assert local.count("try Task.checkCancellation()") >= 3


def test_cancelled_local_transcription_never_turns_into_an_audio_upload():
    home = _read("App/ContentView.swift")
    local = _read("App/LocalSpeechTranscriber.swift")

    assert "static func transcribe(fileURL: URL) async throws" in local
    assert "Task.checkCancellation()" in local
    assert "catch is CancellationError" in local
    assert "try await makeLocalVoiceTextCapture" in home
    assert "try Task.checkCancellation()" in home
    assert "catch is CancellationError" in home


def test_voice_prefers_on_device_transcription_and_keeps_server_audio_as_fallback():
    home = _read("App/ContentView.swift")
    local = _read("App/LocalSpeechTranscriber.swift")
    outbox = _read("App/VoiceCaptureOutbox.swift")

    assert "makeLocalVoiceTextCapture" in home
    assert "LocalSpeechTranscriber.transcribe" in home
    assert "sendTranscribedVoiceCapture" in home
    assert "client.voiceIntake(" in home
    assert "static func url(for item: QueuedVoiceCapture)" in outbox
    assert "SpeechAnalyzer" in local
    assert "SpeechTranscriber" in local
    assert "DictationTranscriber" in local
    assert "AssetInventory.assetInstallationRequest" in local
    assert 'Locale(identifier: "ru-RU")' in local


def test_voice_capture_keeps_transcript_for_retry_when_classification_is_deferred():
    home = _read("App/ContentView.swift")
    outbox = _read("App/CaptureOutbox.swift")

    assert "id: UUID = UUID()" in outbox
    assert "id: queued.id" in home
    assert "id: item.id" in home
    assert 'response.status == "stored"' in home
    assert "CaptureOutbox.enqueue(" in home


def test_voice_outbox_recovers_recorder_temp_file_when_durable_move_never_happened():
    outbox = _read("App/VoiceCaptureOutbox.swift")
    home = _read("App/ContentView.swift")

    assert "static func recoverTemporaryRecordings()" in outbox
    assert 'basename.hasPrefix("assistant-voice-")' in outbox
    assert 'dropFirst("assistant-voice-".count)' in outbox
    assert "fileManager.moveItem(at: url, to: destination)" in outbox
    assert "VoiceCaptureOutbox.recoverTemporaryRecordings()" in home


def test_voice_outbox_recovers_audio_orphaned_between_file_move_and_metadata_write():
    outbox = _read("App/VoiceCaptureOutbox.swift")
    home = _read("App/ContentView.swift")

    assert "private static func storedItems()" in outbox
    assert "contentsOfDirectory(" in outbox
    assert "UUID(uuidString:" in outbox
    assert "recovered.append(" in outbox
    assert "var items = all()" in outbox
    assert "rather than deleting the only audio copy" in home


def test_voice_recording_is_persisted_before_app_can_leave_foreground():
    home = _read("App/ContentView.swift")
    recorder = _read("App/VoiceRecorder.swift")

    assert "else if voiceRecorder.isRecording" in home
    assert "persistInterruptedVoiceCapture()" in home
    assert "VoiceCaptureOutbox.enqueue(" in home
    assert "try? session.setActive(false" in recorder


def test_background_replays_do_not_clear_an_unrelated_clarification():
    home = _read("App/ContentView.swift")

    assert "mayResolveClarification: Bool = true" in home
    assert "item.context == clarificationContext" in home
    assert "else if mayResolveClarification" in home


def test_clarification_can_be_cancelled_explicitly():
    home = _read("App/ContentView.swift")

    assert 'accessibilityLabel("Отменить уточнение")' in home
    assert "private func clearClarification()" in home


def test_one_nonretryable_outbox_item_does_not_block_newer_captures_forever():
    home = _read("App/ContentView.swift")

    voice_start = home.index("private func flushVoiceOutbox")
    text_start = home.index("private func flushOutbox")
    pending_start = home.index("private func isRetryable", text_start)

    voice_block = home[voice_start:text_start]
    text_block = home[text_start:pending_start]

    assert "if isRetryable(error)" in voice_block
    assert "continue" in voice_block
    assert "if isRetryable(error)" in text_block
    assert "continue" in text_block


def test_failed_text_capture_remains_durable_for_later_retry():
    home = _read("App/ContentView.swift")

    assert '"Не удалось отправить. Запись сохранена на телефоне."' in home
    capture_start = home.index("private func capture() async")
    apply_start = home.index("private func applyIntakeResponse", capture_start)
    capture_block = home[capture_start:apply_start]
    catch_block = capture_block[capture_block.rindex("catch {"):]
    assert "CaptureOutbox.remove(queued.id)" not in catch_block


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
    assert "CaptureLaunchSignal.mode(from: notification)" in home
    assert "activateCapture(mode: mode)" in home

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


def test_widget_marks_cached_fallback_as_stale_without_hiding_useful_content():
    widget = _read("Widget/AssistantWidget.swift")

    assert "let stale: Bool" in widget
    assert "cachedEntry(stale: true)" in widget
    assert 'Image(systemName: "wifi.slash")' in widget
    assert 'accessibilityLabel("Показаны сохранённые данные")' in widget
    assert "entry.error != nil || entry.calendarPending || entry.stale" in widget


def test_widget_optimistic_actions_refresh_from_server_after_success():
    shared = _read("Shared/WidgetSharedSettings.swift")
    widget = _read("Widget/AssistantWidget.swift")

    assert "static func clearCachedTodayPreference()" in shared
    assert widget.count("WidgetSharedSettings.clearCachedTodayPreference()") >= 4
    assert widget.count('WidgetCenter.shared.reloadTimelines(ofKind: "AssistantPocketWidget")') >= 8


def test_widget_does_not_ask_for_manual_refresh_during_normal_operation():
    widget = _read("Widget/AssistantWidget.swift")

    assert "if entry.error != nil || entry.stale" in widget
    assert "RefreshAssistantWidgetIntent" in widget


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
