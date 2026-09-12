import SwiftUI

struct ContentView: View {
    @EnvironmentObject private var settings: AppSettings

    @State private var captureText = ""
    @State private var tasks: [TodayTask] = []
    @State private var reminders: [TodayReminder] = []
    @State private var isLoading = false
    @State private var isSending = false
    @State private var errorMessage: String?
    @State private var confirmation: String?
    @State private var showSettings = false

    var body: some View {
        NavigationStack {
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 24) {
                    captureCard

                    if let confirmation {
                        Label(confirmation, systemImage: "checkmark.circle.fill")
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                            .transition(.opacity)
                    }

                    if let errorMessage {
                        Label(errorMessage, systemImage: "exclamationmark.triangle")
                            .font(.subheadline)
                            .foregroundStyle(.red)
                    }

                    todaySection
                }
                .padding(20)
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
            }
            .sheet(isPresented: $showSettings, onDismiss: {
                Task { await loadToday() }
            }) {
                SettingsView()
                    .environmentObject(settings)
            }
        }
    }

    private var captureCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Что нужно запомнить?")
                .font(.title2.weight(.semibold))

            TextField("Например: проверить расчёт Багратиона", text: $captureText, axis: .vertical)
                .lineLimit(2...6)
                .textFieldStyle(.plain)
                .padding(14)
                .background(.quaternary, in: RoundedRectangle(cornerRadius: 14))
                .submitLabel(.send)
                .onSubmit {
                    Task { await capture() }
                }

            HStack {
                Text("Можно использовать диктовку клавиатуры iOS")
                    .font(.caption)
                    .foregroundStyle(.secondary)

                Spacer()

                Button {
                    Task { await capture() }
                } label: {
                    if isSending {
                        ProgressView()
                            .controlSize(.small)
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

    @ViewBuilder
    private var todaySection: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("На сегодня")
                    .font(.headline)
                Spacer()
                if isLoading {
                    ProgressView()
                        .controlSize(.small)
                }
            }

            if !settings.isConfigured {
                Button("Настроить подключение") {
                    showSettings = true
                }
                .buttonStyle(.borderedProminent)
            } else if !isLoading && tasks.isEmpty && reminders.isEmpty {
                Text("Ничего срочного.")
                    .foregroundStyle(.secondary)
                    .padding(.vertical, 8)
            } else {
                ForEach(tasks) { task in
                    taskRow(task)
                    Divider()
                }

                ForEach(reminders) { reminder in
                    reminderRow(reminder)
                    Divider()
                }
            }
        }
    }

    private func taskRow(_ task: TodayTask) -> some View {
        HStack(alignment: .top, spacing: 12) {
            Button {
                Task { await complete(task) }
            } label: {
                Image(systemName: "circle")
                    .font(.title3)
                    .padding(.top, 2)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Выполнено")

            VStack(alignment: .leading, spacing: 5) {
                Text(task.title)
                    .font(.body)
                    .foregroundStyle(.primary)

                HStack(spacing: 6) {
                    if !task.project.isEmpty && task.project.uppercased() != "INBOX" {
                        Text(task.project)
                    }
                    if let deadline = task.deadline {
                        Text(deadline, format: .dateTime.hour().minute())
                            .foregroundStyle(task.overdue ? .red : .secondary)
                    }
                    if !task.assignee.isEmpty {
                        Text(task.assignee)
                    }
                }
                .font(.caption)
                .foregroundStyle(.secondary)
            }

            Spacer(minLength: 0)
        }
        .contentShape(Rectangle())
        .padding(.vertical, 3)
    }

    private func reminderRow(_ reminder: TodayReminder) -> some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: "bell")
                .font(.title3)
                .foregroundStyle(.secondary)
                .padding(.top, 2)

            VStack(alignment: .leading, spacing: 5) {
                Text(reminder.text)
                if let at = reminder.at {
                    Text(at, format: .dateTime.hour().minute())
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(.vertical, 3)
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
            errorMessage = error.localizedDescription
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
            confirmation = "Сохранено"
        } catch {
            errorMessage = error.localizedDescription
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
            errorMessage = error.localizedDescription
        }
    }
}

private struct SettingsView: View {
    @EnvironmentObject private var settings: AppSettings
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            Form {
                Section("Сервер") {
                    TextField("https://assistant.example.com", text: $settings.baseURL)
                        .textInputAutocapitalization(.never)
                        .keyboardType(.URL)
                        .autocorrectionDisabled()

                    SecureField("COMPANION_API_TOKEN", text: $settings.token)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                }

                Section {
                    Text("Приложение использует только отдельный companion API. Токен хранится в Keychain этого iPhone.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
            }
            .navigationTitle("Подключение")
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
