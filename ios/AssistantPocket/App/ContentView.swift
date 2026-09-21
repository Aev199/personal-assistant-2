import Combine
import SwiftUI

struct ContentView: View {
    @EnvironmentObject private var settings: AppSettings
    @Environment(\.scenePhase) private var scenePhase

    @State private var captureText = ""
    @State private var tasks: [TodayTask] = []
    @State private var reminders: [TodayReminder] = []
    @State private var isLoading = false
    @State private var isSending = false
    @State private var errorMessage: String?
    @State private var confirmation: String?
    @State private var showSettings = false
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
                if settings.isConfigured {
                    await loadToday()
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
                }
            }
            .sheet(isPresented: $showSettings, onDismiss: {
                Task { await loadToday() }
            }) {
                SettingsView()
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
                TextField("Написать или надиктовать…", text: $captureText, axis: .vertical)
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
        defer { isSending = false }

        do {
            let client = APIClient(baseURL: settings.normalizedBaseURL, token: settings.token)
            _ = try await client.capture(text)
            captureText = ""
            captureFocused = false
            confirmation = "Записано"
            await loadToday()
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
                        dismiss()
                    }
                    .disabled(!settings.isConfigured)
                }
            }
        }
    }
}
