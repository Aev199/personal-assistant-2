import Combine
import SwiftUI
import WidgetKit

struct ContentView: View {
    let refreshToken: Int
    let onChanged: () -> Void

    @EnvironmentObject private var settings: AppSettings
    @Environment(\.scenePhase) private var scenePhase

    @AppStorage("assistant.captureDraft") private var captureText = ""
    @AppStorage("assistant.lastOpenedAt") private var lastOpenedAt: Double = 0
    @State private var tasks: [TodayTask] = []
    @State private var reminders: [TodayReminder] = []
    @State private var events: [TodayEvent] = []
    @State private var isLoading = false
    @State private var isSending = false
    @State private var errorMessage: String?
    @State private var actionError: String?
    @State private var confirmation: String?
    @State private var confirmationRevision = 0
    @State private var showSettings = false
    @AppStorage("assistant.clarificationContext") private var clarificationContext = ""
    @AppStorage("assistant.clarificationPrompt") private var clarificationPrompt = ""
    @State private var pendingIntake: [NativeIntakePending] = []
    @State private var isFlushingOutbox = false
    @State private var isFlushingVoiceOutbox = false
    @State private var isVoiceSending = false
    @StateObject private var voiceRecorder = VoiceRecorder()
    @State private var editingTask: TodayTask?
    @State private var showFocusPicker = false
    @State private var now = Date()
    @State private var startHelpTaskID: Int?
    @State private var startHelpSteps: [String] = []
    @State private var startHelpCache: [Int: [String]] = [:]
    @State private var isLoadingStartHelp = false
    @State private var startHelpError: String?
    @State private var returningAfterBreak = false
    @FocusState private var captureFocused: Bool

    private let clock = Timer.publish(every: 30, on: .main, in: .common).autoconnect()

    private var manualFocusTask: TodayTask? {
        tasks.first(where: { $0.isFocused })
    }

    private var activeEvent: TodayEvent? {
        events.first { $0.start <= now && $0.end > now }
    }

    private var dueReminder: TodayReminder? {
        reminders.first(where: { reminder in
            guard let at = reminder.at else { return false }
            return at <= now
        })
    }

    private var upcomingEvent: TodayEvent? {
        guard manualFocusTask == nil, activeEvent == nil, dueReminder == nil else { return nil }
        let cutoff = now.addingTimeInterval(15 * 60)
        return events.first { $0.start > now && $0.start <= cutoff && $0.end > now }
    }

    private var focusEvent: TodayEvent? {
        if let activeEvent { return activeEvent }
        if let upcomingEvent { return upcomingEvent }
        return nil
    }

    private var focusReminder: TodayReminder? {
        guard activeEvent == nil else { return nil }
        return dueReminder
    }

    private var focusTask: TodayTask? {
        guard focusEvent == nil, focusReminder == nil else { return nil }
        return manualFocusTask
    }

    private var suggestedFocusTask: TodayTask? {
        let startOfTomorrow = Calendar.current.startOfDay(for: now).addingTimeInterval(24 * 60 * 60)
        return tasks.first { task in
            guard !task.isFocused else { return false }
            guard let deadline = task.deadline else { return true }
            return task.overdue || deadline < startOfTomorrow
        }
    }

    private var remainingTasks: [TodayTask] {
        guard let focusTask else { return tasks }
        return tasks.filter { $0.id != focusTask.id }
    }

    private var remainingReminders: [TodayReminder] {
        guard let focusReminder else { return reminders }
        return reminders.filter { $0.id != focusReminder.id }
    }

    private var remainingEvents: [TodayEvent] {
        let activeOrFuture = events.filter { $0.end > now }
        guard let focusEvent else { return activeOrFuture }
        return activeOrFuture.filter { $0.id != focusEvent.id }
    }

    private var nextEvent: TodayEvent? {
        remainingEvents.first
    }

    private var nextReminder: TodayReminder? {
        remainingReminders.first
    }

    private var nextTasks: [TodayTask] {
        let reservedTimed = (nextEvent == nil ? 0 : 1) + (nextReminder == nil ? 0 : 1)
        return Array(remainingTasks.prefix(max(0, 4 - reservedTimed)))
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 26) {
                    focusSection
                    nextSection
                    captureSection

                    if let confirmation {
                        Label(confirmation, systemImage: "checkmark.circle.fill")
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                            .transition(.opacity)
                    }

                    if let errorMessage {
                        Label(errorMessage, systemImage: "wifi.exclamationmark")
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                    }
                }
                .padding(.horizontal, 20)
                .padding(.vertical, 18)
            }
            .navigationTitle("Сегодня")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button {
                        showSettings = true
                    } label: {
                        Image(systemName: "gearshape")
                    }
                    .accessibilityLabel("Настройки")
                }
            }
            .refreshable {
                await loadToday()
            }
            .task {
                detectReturnGap()
                if settings.isConfigured {
                    await loadToday()
                    await loadPendingIntake()
                    await flushOutbox()
                    await flushVoiceOutbox()
                } else {
                    showSettings = true
                }
                consumeSystemCaptureRequest()
            }
            .onOpenURL { url in
                guard url.scheme == "assistantpocket", url.host == "capture" else { return }
                let mode = URLComponents(url: url, resolvingAgainstBaseURL: false)?
                    .queryItems?
                    .first(where: { $0.name == "mode" })?
                    .value
                if mode == "voice" {
                    activateVoiceCapture()
                } else {
                    activateCapture()
                }
            }
            .onReceive(NotificationCenter.default.publisher(for: CaptureLaunchSignal.notification)) { _ in
                activateCapture()
            }
            .onReceive(clock) { tick in
                now = tick
            }
            .onChange(of: scenePhase) { phase in
                if phase == .active {
                    lastOpenedAt = Date().timeIntervalSince1970
                    consumeSystemCaptureRequest()
                    Task {
                        await loadPendingIntake()
                        await flushOutbox()
                        await flushVoiceOutbox()
                    }
                }
            }
            .onChange(of: refreshToken) { _, _ in
                Task { await loadToday() }
            }
            .alert("Не удалось выполнить действие", isPresented: actionErrorPresented) {
                Button("OK", role: .cancel) {}
            } message: {
                Text(actionError ?? "")
            }
            .sheet(isPresented: $showSettings, onDismiss: {
                Task { await loadToday() }
            }) {
                SettingsView()
                    .environmentObject(settings)
            }
            .sheet(isPresented: $showFocusPicker) {
                FocusPickerView(currentTaskID: manualFocusTask?.id) {
                    onChanged()
                    Task { await loadToday() }
                }
                .environmentObject(settings)
            }
            .sheet(item: $editingTask) { task in
                TaskEditView(task: task) {
                    forgetStartHelp(for: task.id)
                    onChanged()
                    Task { await loadToday() }
                }
                .environmentObject(settings)
            }
        }
    }

    @ViewBuilder
    private var focusSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("Сейчас")
                    .font(.headline)

                Spacer()

                if focusTask != nil {
                    Button("Изменить") {
                        showFocusPicker = true
                    }
                    .font(.subheadline.weight(.medium))
                    .buttonStyle(.plain)
                    .foregroundStyle(.secondary)
                    .accessibilityLabel("Изменить текущую задачу")
                }

                if isLoading {
                    ProgressView()
                        .controlSize(.small)
                }
            }

            if let event = focusEvent {
                eventFocusCard(event)
            } else if let reminder = focusReminder {
                reminderFocusCard(reminder)
            } else if let task = focusTask {
                focusTaskCard(task)
            } else if settings.isConfigured && !isLoading {
                VStack(alignment: .leading, spacing: 10) {
                    Text(
                        suggestedFocusTask == nil
                            ? "На сейчас ничего нет"
                            : (returningAfterBreak ? "Продолжить с одной задачи" : "Ничего не выбрано")
                    )
                    .font(.title3.weight(.semibold))

                    if let suggestedFocusTask {
                        Button {
                            Task { await focus(suggestedFocusTask) }
                        } label: {
                            HStack(spacing: 8) {
                                Image(systemName: "arrow.right.circle.fill")
                                Text("Начать: \(suggestedFocusTask.title)")
                                    .lineLimit(1)
                            }
                        }
                        .buttonStyle(.borderedProminent)
                        .accessibilityLabel("Начать задачу \(suggestedFocusTask.title)")

                        Button("Выбрать другую") {
                            showFocusPicker = true
                        }
                        .font(.subheadline.weight(.medium))
                        .buttonStyle(.plain)
                        .foregroundStyle(.secondary)
                    }
                }
                .padding(16)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(.quaternary, in: RoundedRectangle(cornerRadius: 16))
            } else if !settings.isConfigured {
                Button("Подключить Assistant") {
                    showSettings = true
                }
                .buttonStyle(.borderedProminent)
            }
        }
    }

    private func focusTaskCard(_ task: TodayTask) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .top, spacing: 12) {
                VStack(alignment: .leading, spacing: 7) {
                    Text(task.title)
                        .font(.title3.weight(.semibold))
                        .fixedSize(horizontal: false, vertical: true)

                    taskMeta(task)
                }
                .contentShape(Rectangle())
                .onTapGesture {
                    editingTask = task
                }

                Spacer(minLength: 8)

                Button {
                    Task { await complete(task) }
                } label: {
                    Image(systemName: "checkmark")
                        .font(.headline)
                        .frame(width: 38, height: 38)
                }
                .buttonStyle(.borderedProminent)
                .buttonBorderShape(.circle)
                .accessibilityLabel("Выполнено")
            }

            HStack(spacing: 14) {
                Button("Не сейчас") {
                    Task { await clearFocus(task) }
                }
                .font(.subheadline.weight(.medium))
                .buttonStyle(.plain)
                .foregroundStyle(.secondary)
                .accessibilityLabel("Убрать задачу из Сейчас")

                if startHelpTaskID != task.id {
                    Button("С чего начать?") {
                        Task { await loadStartHelp(task) }
                    }
                    .font(.subheadline.weight(.medium))
                    .buttonStyle(.plain)
                }
            }

            if startHelpTaskID == task.id {
                VStack(alignment: .leading, spacing: 8) {
                    if isLoadingStartHelp {
                        HStack(spacing: 8) {
                            ProgressView()
                                .controlSize(.small)
                            Text("Ищу первый шаг…")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    } else if !startHelpSteps.isEmpty {
                        Text("Первый шаг")
                            .font(.caption.weight(.semibold))
                            .foregroundStyle(.secondary)

                        ForEach(Array(startHelpSteps.enumerated()), id: \.offset) { index, step in
                            HStack(alignment: .top, spacing: 7) {
                                Text(index == 0 ? "→" : "·")
                                    .foregroundStyle(.secondary)
                                Text(step)
                                    .font(index == 0 ? .subheadline.weight(.medium) : .subheadline)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }

                        Button("Скрыть") {
                            clearStartHelp()
                        }
                        .font(.caption.weight(.medium))
                        .buttonStyle(.plain)
                        .foregroundStyle(.secondary)
                    } else if let startHelpError {
                        HStack(spacing: 8) {
                            Text(startHelpError)
                                .font(.caption)
                                .foregroundStyle(.secondary)

                            Button("Повторить") {
                                Task { await loadStartHelp(task) }
                            }
                            .font(.caption.weight(.medium))
                            .buttonStyle(.plain)
                        }
                    }
                }
                .padding(.top, 2)
            }
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.quaternary, in: RoundedRectangle(cornerRadius: 16))
    }

    private func eventFocusCard(_ event: TodayEvent) -> some View {
        HStack(alignment: .top, spacing: 12) {
            VStack(alignment: .leading, spacing: 7) {
                Label("Календарь", systemImage: "calendar")
                    .font(.caption.weight(.medium))
                    .foregroundStyle(.secondary)
                Text(event.title)
                    .font(.title3.weight(.semibold))
                    .fixedSize(horizontal: false, vertical: true)
                Text(event.start, format: .dateTime.hour().minute())
                    + Text("–")
                    + Text(event.end, format: .dateTime.hour().minute())

                if let paused = manualFocusTask {
                    Text("После встречи: \(paused.title)")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
            }
            .font(.subheadline)

            Spacer(minLength: 8)

            Button {
                Task { await dismiss(event) }
            } label: {
                Image(systemName: "xmark.circle.fill")
                    .font(.title3)
                    .foregroundStyle(.secondary)
                    .frame(width: 38, height: 38)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Убрать встречу из внимания")
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.quaternary, in: RoundedRectangle(cornerRadius: 16))
    }

    private func reminderFocusCard(_ reminder: TodayReminder) -> some View {
        HStack(alignment: .top, spacing: 12) {
            VStack(alignment: .leading, spacing: 7) {
                Label("Напоминание", systemImage: "bell.fill")
                    .font(.caption.weight(.medium))
                    .foregroundStyle(.secondary)
                Text(reminder.text)
                    .font(.title3.weight(.semibold))
                if let at = reminder.at {
                    Text(at, format: .dateTime.hour().minute())
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }
            }

            Spacer(minLength: 0)

            Button("+15") {
                Task { await snooze(reminder) }
            }
            .buttonStyle(.bordered)
            .buttonBorderShape(.capsule)
            .accessibilityLabel("Отложить на 15 минут")
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.quaternary, in: RoundedRectangle(cornerRadius: 16))
    }

    @ViewBuilder
    private var nextSection: some View {
        if !nextTasks.isEmpty || nextEvent != nil || nextReminder != nil {
            VStack(alignment: .leading, spacing: 10) {
                Text("Дальше")
                    .font(.headline)

                nextTimedRows

                ForEach(nextTasks) { task in
                    compactTaskRow(task)
                }
            }
        }
    }

    @ViewBuilder
    private var nextTimedRows: some View {
        if let event = nextEvent, let reminder = nextReminder, let reminderAt = reminder.at {
            if reminderAt <= event.start {
                compactReminderRow(reminder)
                compactEventRow(event)
            } else {
                compactEventRow(event)
                compactReminderRow(reminder)
            }
        } else {
            if let event = nextEvent {
                compactEventRow(event)
            }
            if let reminder = nextReminder {
                compactReminderRow(reminder)
            }
        }
    }

    private func compactTaskRow(_ task: TodayTask) -> some View {
        HStack(alignment: .top, spacing: 11) {
            Button {
                Task { await complete(task) }
            } label: {
                Image(systemName: "circle")
                    .font(.title3)
                    .padding(.top, 1)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Выполнено")

            VStack(alignment: .leading, spacing: 4) {
                Text(task.title)
                    .font(.body)
                    .lineLimit(2)

                taskMeta(task)

                if task.isFocused && focusTask == nil {
                    Text("↩︎ вернуться")
                        .font(.caption.weight(.medium))
                        .foregroundStyle(.secondary)
                }
            }
            .contentShape(Rectangle())
            .onTapGesture {
                editingTask = task
            }

            Spacer(minLength: 0)
        }
        .padding(.vertical, 5)
    }

    private func compactEventRow(_ event: TodayEvent) -> some View {
        HStack(alignment: .top, spacing: 11) {
            Image(systemName: "calendar")
                .font(.body)
                .foregroundStyle(.secondary)
                .padding(.top, 2)

            VStack(alignment: .leading, spacing: 4) {
                Text(event.title)
                    .font(.body)
                    .lineLimit(2)
                Text(event.start, format: .dateTime.hour().minute())
                    + Text("–")
                    + Text(event.end, format: .dateTime.hour().minute())

                if let relative = timeUntilText(event.start) {
                    Text(relative)
                }
            }
            .font(.caption)
            .foregroundStyle(.secondary)

            Spacer(minLength: 0)

            Button {
                Task { await dismiss(event) }
            } label: {
                Image(systemName: "xmark.circle")
                    .font(.body)
                    .foregroundStyle(.secondary)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Убрать встречу из внимания")
        }
        .padding(.vertical, 5)
    }

    private func timeUntilText(_ date: Date) -> String? {
        let seconds = date.timeIntervalSince(now)
        guard seconds > 0 else { return nil }

        let minutes = max(1, Int((seconds / 60).rounded(.up)))
        if minutes < 60 {
            return "через \(minutes) мин"
        }

        let hours = minutes / 60
        let rest = minutes % 60
        guard hours <= 2 else { return nil }
        return rest == 0 ? "через \(hours) ч" : "через \(hours) ч \(rest) мин"
    }

    private func compactReminderRow(_ reminder: TodayReminder) -> some View {
        HStack(alignment: .top, spacing: 11) {
            Image(systemName: "bell")
                .font(.body)
                .foregroundStyle(.secondary)
                .padding(.top, 2)

            VStack(alignment: .leading, spacing: 4) {
                Text(reminder.text)
                    .font(.body)
                    .lineLimit(2)
                if let at = reminder.at {
                    Text(at, format: .dateTime.hour().minute())
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }

            Spacer(minLength: 0)

            Button("+15") {
                Task { await snooze(reminder) }
            }
            .buttonStyle(.plain)
            .font(.caption.weight(.medium))
            .foregroundStyle(.secondary)
            .accessibilityLabel("Отложить на 15 минут")
        }
        .padding(.vertical, 5)
    }

    @ViewBuilder
    private func taskMeta(_ task: TodayTask) -> some View {
        HStack(spacing: 6) {
            if task.isPersonal {
                Text("Личное")
            } else if !task.project.isEmpty && task.project.uppercased() != "INBOX" {
                Text(task.project)
            }

            if let deadline = task.deadline {
                if task.overdue {
                    Text(taskDeadlineText(deadline))
                        .foregroundStyle(.red)
                } else {
                    Text(taskDeadlineText(deadline))
                        .foregroundStyle(.secondary)
                }
            }

            if !task.assignee.isEmpty {
                Text(task.assignee)
            }

            if task.isFocused, let focusedSince = task.focusedSince {
                Text("с")
                Text(focusedSince, format: .dateTime.hour().minute())
            }
        }
        .font(.caption)
        .foregroundStyle(.secondary)
    }

    private var captureSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Запомнить")
                .font(.headline)

            HStack(alignment: .bottom, spacing: 10) {
                TextField(
                    voiceRecorder.isRecording
                        ? "Говорите…"
                        : (clarificationPrompt.isEmpty ? "Написать или надиктовать…" : "Уточнить…"),
                    text: $captureText,
                    axis: .vertical
                )
                    .lineLimit(1...4)
                    .textFieldStyle(.plain)
                    .focused($captureFocused)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 12)
                    .background(.quaternary, in: RoundedRectangle(cornerRadius: 14))
                    .submitLabel(.send)
                    .onSubmit {
                        Task { await capture() }
                    }
                    .disabled(voiceRecorder.isRecording || isVoiceSending)

                Button {
                    Task {
                        if voiceRecorder.isRecording {
                            await finishVoiceCapture()
                        } else {
                            await startVoiceCapture()
                        }
                    }
                } label: {
                    if isVoiceSending {
                        ProgressView()
                            .controlSize(.small)
                            .frame(width: 22, height: 22)
                    } else {
                        Image(systemName: voiceRecorder.isRecording ? "stop.fill" : "mic.fill")
                            .font(.headline)
                    }
                }
                .buttonStyle(.bordered)
                .buttonBorderShape(.circle)
                .disabled(isSending || isVoiceSending)
                .accessibilityLabel(voiceRecorder.isRecording ? "Остановить и отправить" : "Записать голосом")

                Button {
                    Task { await capture() }
                } label: {
                    if isSending {
                        ProgressView()
                            .controlSize(.small)
                            .frame(width: 22, height: 22)
                    } else {
                        Image(systemName: "arrow.up")
                            .font(.headline)
                    }
                }
                .buttonStyle(.borderedProminent)
                .buttonBorderShape(.circle)
                .disabled(
                    captureText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                    || isSending
                    || isVoiceSending
                    || voiceRecorder.isRecording
                )
                .accessibilityLabel("Сохранить")
            }

            if !clarificationPrompt.isEmpty {
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: "questionmark.circle")
                        .foregroundStyle(.secondary)
                    Text(clarificationPrompt)
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }

            ForEach(pendingIntake) { pending in
                VStack(alignment: .leading, spacing: 10) {
                    Text(pending.title)
                        .font(.subheadline.weight(.semibold))
                        .fixedSize(horizontal: false, vertical: true)

                    if let start = pending.payload?.startLocal {
                        HStack(spacing: 6) {
                            Text(start, format: .dateTime.day().month().hour().minute())
                            if let duration = pending.payload?.durationMin {
                                Text("·")
                                Text("\(duration) мин")
                            }
                        }
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    }

                    HStack(spacing: 10) {
                        Button("Добавить") {
                            Task { await confirmPending(pending) }
                        }
                        .buttonStyle(.borderedProminent)

                        Button("Не добавлять") {
                            Task { await cancelPending(pending) }
                        }
                        .buttonStyle(.bordered)
                    }
                }
                .padding(12)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(.quaternary, in: RoundedRectangle(cornerRadius: 14))
            }
        }
    }

    private func consumeSystemCaptureRequest() {
        guard CaptureLaunchSignal.consume() else { return }
        activateCapture()
    }

    private func activateVoiceCapture() {
        confirmation = nil
        errorMessage = nil
        guard settings.isConfigured else {
            showSettings = true
            return
        }
        captureFocused = false
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.18) {
            Task { await startVoiceCapture() }
        }
    }

    private func activateCapture() {
        confirmation = nil
        errorMessage = nil
        guard settings.isConfigured else {
            showSettings = true
            return
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.12) {
            captureFocused = true
        }
    }

    @MainActor
    private func loadToday() async {
        guard settings.isConfigured else { return }
        isLoading = true
        errorMessage = nil
        defer { isLoading = false }

        do {
            let client = APIClient(baseURL: settings.normalizedBaseURL, token: settings.token)
            let response = try await client.loadToday()
            tasks = response.tasks
            reminders = response.reminders
            events = response.events ?? []
        } catch {
            present(error)
        }
    }

    @MainActor
    private func loadPendingIntake() async {
        guard settings.isConfigured else { return }
        do {
            let client = APIClient(baseURL: settings.normalizedBaseURL, token: settings.token)
            pendingIntake = try await client.loadPendingIntake().pending
        } catch {
            // A background restore failure should not interrupt the Today surface.
        }
    }

    @MainActor
    private func startVoiceCapture() async {
        guard settings.isConfigured else {
            showSettings = true
            return
        }
        guard !voiceRecorder.isRecording, !isVoiceSending else { return }

        confirmation = nil
        errorMessage = nil
        captureFocused = false

        do {
            try await voiceRecorder.start()
        } catch {
            present(error)
        }
    }

    @MainActor
    private func finishVoiceCapture() async {
        guard let recordingURL = voiceRecorder.stop() else { return }
        let context = clarificationContext.isEmpty ? nil : clarificationContext

        do {
            let queued = try VoiceCaptureOutbox.enqueue(
                recordingURL: recordingURL,
                context: context
            )
            await sendVoiceCapture(queued)
        } catch {
            try? FileManager.default.removeItem(at: recordingURL)
            present(error)
        }
    }

    @MainActor
    private func sendVoiceCapture(_ queued: QueuedVoiceCapture) async {
        guard settings.isConfigured else { return }

        isVoiceSending = true
        errorMessage = nil
        confirmation = nil
        defer { isVoiceSending = false }

        do {
            let audioData = try VoiceCaptureOutbox.data(for: queued)
            let client = APIClient(
                baseURL: settings.normalizedBaseURL,
                token: settings.token
            )
            let response = try await client.voiceIntake(
                audioData: audioData,
                context: queued.context,
                clientID: queued.id
            )

            VoiceCaptureOutbox.remove(queued.id)

            if response.status == "stored" {
                presentConfirmation("Голос записан, разберу позже")
                return
            }

            await applyIntakeResponse(
                response,
                originalText: response.transcript ?? "Голосовая запись"
            )
        } catch {
            if isRetryable(error) {
                presentConfirmation("Голос сохранён на телефоне")
                return
            }

            // Keep the recording in the outbox even for an unexpected backend
            // error. A capture must never disappear silently.
            present(error)
        }
    }

    @MainActor
    private func flushVoiceOutbox() async {
        guard settings.isConfigured, !isFlushingVoiceOutbox, !voiceRecorder.isRecording else {
            return
        }

        let queued = VoiceCaptureOutbox.all()
        guard !queued.isEmpty else { return }

        isFlushingVoiceOutbox = true
        defer { isFlushingVoiceOutbox = false }

        let client = APIClient(
            baseURL: settings.normalizedBaseURL,
            token: settings.token
        )

        for item in queued {
            do {
                let response = try await client.voiceIntake(
                    audioData: VoiceCaptureOutbox.data(for: item),
                    context: item.context,
                    clientID: item.id
                )
                VoiceCaptureOutbox.remove(item.id)

                if response.status != "stored" {
                    await applyIntakeResponse(
                        response,
                        originalText: response.transcript ?? "Голосовая запись"
                    )
                }
            } catch {
                break
            }
        }
    }

    @MainActor
    private func capture() async {
        let text = captureText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        guard settings.isConfigured else {
            showSettings = true
            return
        }

        isSending = true
        errorMessage = nil
        confirmation = nil
        let context = clarificationContext.isEmpty ? nil : clarificationContext
        let queued = CaptureOutbox.enqueue(text: text, context: context)
        defer { isSending = false }

        do {
            let client = APIClient(baseURL: settings.normalizedBaseURL, token: settings.token)
            let response = try await client.intake(text, context: context, clientID: queued.id)
            captureText = ""

            if response.status == "stored" {
                captureFocused = false
                presentConfirmation("Сохранено, разберу позже")
                return
            }

            CaptureOutbox.remove(queued.id)
            await applyIntakeResponse(response, originalText: text)
        } catch {
            if isRetryable(error) {
                captureText = ""
                captureFocused = false
                presentConfirmation("Сохранено на телефоне")
                return
            }

            CaptureOutbox.remove(queued.id)
            present(error)
        }
    }

    @MainActor
    private func applyIntakeResponse(_ response: NativeIntakeResponse, originalText: String) async {
        if let need = response.needsInput.first {
            clarificationPrompt = need.prompt
            clarificationContext = response.context ?? (clarificationContext.isEmpty ? originalText : clarificationContext)
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.08) {
                captureFocused = true
            }
        } else {
            clarificationPrompt = ""
            clarificationContext = ""
            captureFocused = false
        }

        for pending in response.pending where !pendingIntake.contains(where: { $0.id == pending.id }) {
            pendingIntake.append(pending)
        }

        let savedCount = response.saved.count
        if savedCount == 1, let item = response.saved.first {
            switch item.kind.lowercased() {
            case "idea":
                presentConfirmation("Идея сохранена")
            case "reminder":
                presentConfirmation("Напоминание создано")
            case "event", "calendar_event":
                presentConfirmation("Встреча добавлена")
            default:
                presentConfirmation("Задача записана")
            }
        } else if savedCount > 1 {
            presentConfirmation("Записано: \(savedCount)")
        } else if response.status == "stored" {
            presentConfirmation(response.message ?? "Записано")
        }

        if savedCount > 0 {
            returningAfterBreak = false
            onChanged()
            WidgetCenter.shared.reloadTimelines(ofKind: "AssistantPocketWidget")
            await loadToday()
        }
    }

    @MainActor
    private func flushOutbox() async {
        guard settings.isConfigured, !isFlushingOutbox else { return }
        let queued = CaptureOutbox.all()
        guard !queued.isEmpty else { return }

        isFlushingOutbox = true
        defer { isFlushingOutbox = false }

        let client = APIClient(baseURL: settings.normalizedBaseURL, token: settings.token)
        var refreshed = false

        for item in queued {
            do {
                let response = try await client.intake(item.text, context: item.context, clientID: item.id)
                if response.status == "stored" {
                    presentConfirmation("Сохранено, разберу позже")
                    break
                }

                CaptureOutbox.remove(item.id)
                await applyIntakeResponse(response, originalText: item.text)
                refreshed = refreshed || !response.saved.isEmpty
            } catch {
                if isRetryable(error) {
                    break
                }
                // Keep the item: a durable capture is preferable to silent loss.
                break
            }
        }

        if refreshed {
            WidgetCenter.shared.reloadTimelines(ofKind: "AssistantPocketWidget")
        }
    }

    private func isRetryable(_ error: Error) -> Bool {
        if let urlError = error as? URLError {
            switch urlError.code {
            case .timedOut, .notConnectedToInternet, .networkConnectionLost,
                 .cannotConnectToHost, .cannotFindHost, .dnsLookupFailed:
                return true
            default:
                return false
            }
        }
        if let apiError = error as? APIClientError,
           case let .http(code, _) = apiError {
            return code == 502 || code == 503 || code == 504
        }
        return false
    }

    @MainActor
    private func confirmPending(_ pending: NativeIntakePending) async {
        errorMessage = nil
        do {
            let client = APIClient(baseURL: settings.normalizedBaseURL, token: settings.token)
            let response = try await client.confirmIntake(pendingActionID: pending.pendingActionId)
            guard response.ok else {
                throw APIClientError.invalidResponse
            }
            withAnimation {
                pendingIntake.removeAll { $0.id == pending.id }
            }
            presentConfirmation("Добавлено")
            WidgetCenter.shared.reloadTimelines(ofKind: "AssistantPocketWidget")
            await loadToday()
        } catch {
            present(error)
        }
    }

    @MainActor
    private func cancelPending(_ pending: NativeIntakePending) async {
        errorMessage = nil
        do {
            let client = APIClient(baseURL: settings.normalizedBaseURL, token: settings.token)
            _ = try await client.cancelIntake(pendingActionID: pending.pendingActionId)
            withAnimation {
                pendingIntake.removeAll { $0.id == pending.id }
            }
        } catch {
            present(error)
        }
    }

    @MainActor
    private func snooze(_ reminder: TodayReminder) async {
        errorMessage = nil
        do {
            let client = APIClient(baseURL: settings.normalizedBaseURL, token: settings.token)
            _ = try await client.snoozeReminder(reminderID: reminder.id, minutes: 15)
            withAnimation {
                reminders.removeAll { $0.id == reminder.id }
            }
            presentConfirmation("Отложено на 15 минут")
            WidgetCenter.shared.reloadTimelines(ofKind: "AssistantPocketWidget")
        } catch {
            presentActionError(error)
        }
    }

    @MainActor
    private func dismiss(_ event: TodayEvent) async {
        errorMessage = nil
        do {
            let client = APIClient(baseURL: settings.normalizedBaseURL, token: settings.token)
            _ = try await client.dismissEvent(eventID: event.id, until: event.end)
            withAnimation {
                events.removeAll { $0.id == event.id }
            }
            WidgetCenter.shared.reloadTimelines(ofKind: "AssistantPocketWidget")
        } catch {
            presentActionError(error)
        }
    }

    @MainActor
    private func detectReturnGap() {
        let current = Date().timeIntervalSince1970
        if lastOpenedAt > 0 {
            returningAfterBreak = current - lastOpenedAt >= 36 * 60 * 60
        }
        lastOpenedAt = current
    }

    @MainActor
    private func loadStartHelp(_ task: TodayTask) async {
        startHelpTaskID = task.id
        startHelpError = nil

        if let cached = startHelpCache[task.id], !cached.isEmpty {
            startHelpSteps = cached
            isLoadingStartHelp = false
            return
        }

        startHelpSteps = []
        isLoadingStartHelp = true
        defer { isLoadingStartHelp = false }

        do {
            let client = APIClient(baseURL: settings.normalizedBaseURL, token: settings.token)
            let response = try await client.taskStartHelp(taskID: task.id)
            guard startHelpTaskID == task.id else { return }
            startHelpSteps = response.steps
            startHelpCache[task.id] = response.steps
        } catch {
            guard startHelpTaskID == task.id else { return }
            startHelpError = "Подсказка сейчас недоступна."
        }
    }

    @MainActor
    private func clearStartHelp() {
        startHelpTaskID = nil
        startHelpSteps = []
        startHelpError = nil
        isLoadingStartHelp = false
    }

    @MainActor
    private func forgetStartHelp(for taskID: Int) {
        startHelpCache.removeValue(forKey: taskID)
        if startHelpTaskID == taskID {
            clearStartHelp()
        }
    }

    @MainActor
    private func focus(_ task: TodayTask) async {
        errorMessage = nil
        do {
            let client = APIClient(baseURL: settings.normalizedBaseURL, token: settings.token)
            _ = try await client.focusTask(taskID: task.id)
            returningAfterBreak = false
            clearStartHelp()
            onChanged()
            await loadToday()
            WidgetCenter.shared.reloadTimelines(ofKind: "AssistantPocketWidget")
        } catch {
            presentActionError(error)
        }
    }

    @MainActor
    private func clearFocus(_ task: TodayTask) async {
        errorMessage = nil
        do {
            let client = APIClient(baseURL: settings.normalizedBaseURL, token: settings.token)
            _ = try await client.clearFocus(taskID: task.id)
            clearStartHelp()
            onChanged()
            await loadToday()
            WidgetCenter.shared.reloadTimelines(ofKind: "AssistantPocketWidget")
        } catch {
            presentActionError(error)
        }
    }

    @MainActor
    private func complete(_ task: TodayTask) async {
        errorMessage = nil
        do {
            let client = APIClient(baseURL: settings.normalizedBaseURL, token: settings.token)
            _ = try await client.markDone(taskID: task.id)
            forgetStartHelp(for: task.id)
            withAnimation {
                tasks.removeAll { $0.id == task.id }
            }
            onChanged()
            WidgetCenter.shared.reloadTimelines(ofKind: "AssistantPocketWidget")
        } catch {
            presentActionError(error)
        }
    }

    @MainActor
    private func presentConfirmation(_ message: String) {
        confirmationRevision += 1
        let revision = confirmationRevision
        withAnimation(.easeOut(duration: 0.15)) {
            confirmation = message
        }

        DispatchQueue.main.asyncAfter(deadline: .now() + 2.4) {
            guard confirmationRevision == revision else { return }
            withAnimation(.easeIn(duration: 0.15)) {
                confirmation = nil
            }
        }
    }

    private var actionErrorPresented: Binding<Bool> {
        Binding(
            get: { actionError != nil },
            set: { isPresented in
                if !isPresented {
                    actionError = nil
                }
            }
        )
    }

    @MainActor
    private func presentActionError(_ error: Error) {
        if error is CancellationError {
            return
        }
        if let urlError = error as? URLError {
            switch urlError.code {
            case .cancelled:
                return
            case .timedOut, .notConnectedToInternet, .networkConnectionLost,
                 .cannotConnectToHost, .cannotFindHost, .dnsLookupFailed:
                actionError = "Нет связи с Assistant."
                return
            default:
                break
            }
        }
        actionError = error.localizedDescription
    }

    @MainActor
    private func present(_ error: Error) {
        if error is CancellationError {
            return
        }
        if let urlError = error as? URLError {
            switch urlError.code {
            case .cancelled:
                return
            case .timedOut, .notConnectedToInternet, .networkConnectionLost, .cannotConnectToHost, .cannotFindHost:
                errorMessage = "Нет связи с Assistant."
                return
            default:
                break
            }
        }
        errorMessage = error.localizedDescription
    }
}

private struct SettingsView: View {
    @EnvironmentObject private var settings: AppSettings
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            Form {
                Section("Подключение") {
                    TextField("Адрес Assistant", text: $settings.baseURL)
                        .textInputAutocapitalization(.never)
                        .keyboardType(.URL)
                        .autocorrectionDisabled()

                    SecureField("Код доступа", text: $settings.token)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                }

                Section {
                    Text("Эти данные нужны только один раз для подключения к вашему Assistant.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
            }
            .navigationTitle("Assistant")
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Готово") {
                        settings.syncWidgetSettings()
                        dismiss()
                    }
                    .disabled(!settings.isConfigured)
                }
            }
        }
    }
}
