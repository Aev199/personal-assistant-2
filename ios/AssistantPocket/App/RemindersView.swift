import SwiftUI
import WidgetKit

/// Upcoming reminders: each can become a task or be deleted.
struct RemindersView: View {
    @Environment(\.dismiss) private var dismiss
    @EnvironmentObject private var settings: AppSettings

    let onChanged: () -> Void

    @State private var reminders: [UpcomingReminder] = []
    @State private var isLoading = false
    @State private var applyingID: Int?
    @State private var errorMessage: String?
    @State private var notice: String?

    var body: some View {
        NavigationStack {
            Group {
                if reminders.isEmpty && isLoading {
                    ProgressView("Загружаю напоминания…")
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                } else if reminders.isEmpty, let errorMessage {
                    ContentUnavailableView(
                        "Не удалось загрузить напоминания",
                        systemImage: "wifi.exclamationmark",
                        description: Text(errorMessage)
                    )
                } else if reminders.isEmpty {
                    ContentUnavailableView("Напоминаний нет", systemImage: "bell.slash")
                } else {
                    List {
                        if let notice {
                            Text(notice)
                                .font(.subheadline)
                                .foregroundStyle(.secondary)
                        }
                        ForEach(reminders) { reminder in
                            row(reminder)
                        }
                    }
                    .listStyle(.plain)
                    .refreshable { await load() }
                }
            }
            .navigationTitle("Напоминания")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Готово") { dismiss() }
                }
            }
            .alert("Не удалось изменить напоминание", isPresented: actionErrorPresented) {
                Button("OK", role: .cancel) {}
            } message: {
                Text(errorMessage ?? "")
            }
            .task { await load() }
        }
    }

    private func row(_ reminder: UpcomingReminder) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(reminder.text)
                .font(.body)
                .fixedSize(horizontal: false, vertical: true)

            HStack(spacing: 6) {
                if let at = reminder.at {
                    Text(taskDeadlineText(at))
                }
                if let repeatLabel = reminder.repeatLabel {
                    Text("· \(repeatLabel)")
                }
            }
            .font(.caption)
            .foregroundStyle(.secondary)

            HStack(spacing: 10) {
                Button("В задачу") {
                    Task { await change(reminder, toTask: true) }
                }
                .buttonStyle(.bordered)
                .accessibilityLabel("Сделать задачей: \(reminder.text)")

                Button("Удалить", role: .destructive) {
                    Task { await change(reminder, toTask: false) }
                }
                .buttonStyle(.bordered)
                .accessibilityLabel("Удалить напоминание: \(reminder.text)")

                if applyingID == reminder.id {
                    ProgressView().controlSize(.small)
                }
            }
            .controlSize(.small)
            .disabled(applyingID != nil)
        }
        .padding(.vertical, 4)
    }

    private var actionErrorPresented: Binding<Bool> {
        Binding(
            get: { errorMessage != nil && !reminders.isEmpty },
            set: { isPresented in
                if !isPresented { errorMessage = nil }
            }
        )
    }

    @MainActor
    private func load() async {
        guard settings.isConfigured else { return }
        isLoading = true
        errorMessage = nil
        defer { isLoading = false }
        do {
            let client = APIClient(baseURL: settings.normalizedBaseURL, token: settings.token)
            reminders = try await client.loadReminders().reminders
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    @MainActor
    private func change(_ reminder: UpcomingReminder, toTask: Bool) async {
        guard applyingID == nil else { return }
        applyingID = reminder.id
        errorMessage = nil
        defer { applyingID = nil }
        do {
            let client = APIClient(baseURL: settings.normalizedBaseURL, token: settings.token)
            _ = try await client.changeReminder(reminderID: reminder.id, toTask: toTask)
            withAnimation {
                reminders.removeAll { $0.id == reminder.id }
            }
            notice = toTask ? "Теперь это задача: \(reminder.text)" : "Удалено: \(reminder.text)"
            WidgetCenter.shared.reloadTimelines(ofKind: "AssistantPocketWidget")
            onChanged()
        } catch let APIClientError.http(code, _) where code == 409 {
            reminders.removeAll { $0.id == reminder.id }
            notice = "Это напоминание уже сработало или удалено."
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}
