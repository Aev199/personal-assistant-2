import Combine
import SwiftUI
import WidgetKit

struct ContentView: View {
    @EnvironmentObject private var settings: AppSettings
    @Environment(\.scenePhase) private var scenePhase

    @AppStorage("assistant.captureDraft") private var captureText = ""
    @State private var tasks: [TodayTask] = []
    @State private var reminders: [TodayReminder] = []
    @State private var isLoading = false
    @State private var isSending = false
    @State private var errorMessage: String?
    @State private var confirmation: String?
    @State private var showSettings = false
    @AppStorage("assistant.clarificationContext") private var clarificationContext = ""
    @AppStorage("assistant.clarificationPrompt") private var clarificationPrompt = ""
    @State private var pendingIntake: [NativeIntakePending] = []
    @State private var isFlushingOutbox = false
    @State private var editingTask: TodayTask?
    @FocusState private var captureFocused: Bool

    private var focusTask: TodayTask? { tasks.first }
    private var nextTasks: [TodayTask] { Array(tasks.dropFirst()) }

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
                ToolbarItemGroup(placement: .topBarTrailing) {
                    NavigationLink {
                        AllTasksView {
                            Task { await loadToday() }
                        }
                        .environmentObject(settings)
                    } label: {
                        Image(systemName: "list.bullet")
                    }
                    .accessibilityLabel("Все задачи")

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
                if settings.isConfigured {
                    await loadToday()
                    await flushOutbox()
                } else {
                    showSettings = true
                }
                consumeSystemCaptureRequest()
            }
            .onOpenURL { url in
                guard url.scheme == "assistantpocket", url.host == "capture" else { return }
                activateCapture()
            }
            .onReceive(NotificationCenter.default.publisher(for: CaptureLaunchSignal.notification)) { _ in
                activateCapture()
            }
            .onChange(of: scenePhase) { phase in
                if phase == .active {
                    consumeSystemCaptureRequest()
                    Task { await flushOutbox() }
                }
            }
            .sheet(isPresented: $showSettings, onDismiss: {
                Task { await loadToday() }
            }) {
                SettingsView()
                    .environmentObject(settings)
            }
            .sheet(item: $editingTask) { task in
                TaskEditView(task: task) {
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
                if isLoading {
                    ProgressView()
                        .controlSize(.small)
                }
            }

            if let task = focusTask {
                focusTaskCard(task)
            } else if let reminder = reminders.first {
                reminderFocusCard(reminder)
            } else if settings.isConfigured && !isLoading {
                VStack(alignment: .leading, spacing: 7) {
                    Text("Ничего обязательного")
                        .font(.title3.weight(.semibold))
                    Text("Можно спокойно выбрать следующее дело или быстро записать новое.")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
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

            if task.inProgress {
                Label("В работе", systemImage: "play.fill")
                    .font(.caption.weight(.medium))
                    .foregroundStyle(.secondary)
            }
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.quaternary, in: RoundedRectangle(cornerRadius: 16))
    }

    private func reminderFocusCard(_ reminder: TodayReminder) -> some View {
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
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.quaternary, in: RoundedRectangle(cornerRadius: 16))
    }

    @ViewBuilder
    private var nextSection: some View {
        let visibleReminders = focusTask == nil ? Array(reminders.dropFirst()) : reminders

        if !nextTasks.isEmpty || !visibleReminders.isEmpty {
            VStack(alignment: .leading, spacing: 10) {
                Text("Дальше")
                    .font(.headline)

                ForEach(nextTasks) { task in
                    compactTaskRow(task)
                }

                ForEach(visibleReminders) { reminder in
                    compactReminderRow(reminder)
                }
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
            }
            .contentShape(Rectangle())
            .onTapGesture {
                editingTask = task
            }

            Spacer(minLength: 0)
        }
        .padding(.vertical, 5)
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
        }
        .padding(.vertical, 5)
    }

    @ViewBuilder
    private func taskMeta(_ task: TodayTask) -> some View {
        HStack(spacing: 6) {
            if !task.project.isEmpty && task.project.uppercased() != "INBOX" {
                Text(task.project)
            }

            if let deadline = task.deadline {
                if task.overdue {
                    Text(deadline, format: .dateTime.hour().minute())
                        .foregroundStyle(.red)
                } else {
                    Text(deadline, format: .dateTime.hour().minute())
                        .foregroundStyle(.secondary)
                }
            }

            if !task.assignee.isEmpty {
                Text(task.assignee)
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
                TextField(clarificationPrompt.isEmpty ? "Написать или надиктовать…" : "Уточнить…", text: $captureText, axis: .vertical)
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
                .disabled(captureText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || isSending)
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
        } catch {
            present(error)
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
            let response = try await client.intake(text, context: context)
            CaptureOutbox.remove(queued.id)
            captureText = ""
            await applyIntakeResponse(response, originalText: text)
        } catch {
            if isRetryable(error) {
                captureText = ""
                captureFocused = false
                confirmation = "Сохранено на телефоне"
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
        if savedCount == 1 {
            confirmation = "Записано"
        } else if savedCount > 1 {
            confirmation = "Записано: \(savedCount)"
        } else if response.status == "stored" {
            confirmation = response.message ?? "Записано"
        }

        if savedCount > 0 {
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
                let response = try await client.intake(item.text, context: item.context)
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
            confirmation = "Добавлено"
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
    private func complete(_ task: TodayTask) async {
        errorMessage = nil
        do {
            let client = APIClient(baseURL: settings.normalizedBaseURL, token: settings.token)
            _ = try await client.markDone(taskID: task.id)
            withAnimation {
                tasks.removeAll { $0.id == task.id }
            }
            WidgetCenter.shared.reloadTimelines(ofKind: "AssistantPocketWidget")
        } catch {
            present(error)
        }
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
