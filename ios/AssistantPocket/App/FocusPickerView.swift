import SwiftUI
import WidgetKit

struct FocusPickerView: View {
    @Environment(\.dismiss) private var dismiss
    @EnvironmentObject private var settings: AppSettings

    let currentTaskID: Int?
    let onChanged: () -> Void

    @State private var tasks: [TodayTask] = []
    @State private var searchText = ""
    @State private var isLoading = false
    @State private var isApplying = false
    @State private var errorMessage: String?

    private var filteredTasks: [TodayTask] {
        let query = searchText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !query.isEmpty else { return tasks }

        return tasks.filter { task in
            task.title.localizedCaseInsensitiveContains(query)
                || task.project.localizedCaseInsensitiveContains(query)
                || task.assignee.localizedCaseInsensitiveContains(query)
        }
    }

    var body: some View {
        NavigationStack {
            Group {
                if tasks.isEmpty && isLoading {
                    ProgressView("Загружаю задачи…")
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                } else if tasks.isEmpty, let errorMessage {
                    ContentUnavailableView(
                        "Не удалось загрузить задачи",
                        systemImage: "wifi.exclamationmark",
                        description: Text(errorMessage)
                    )
                } else if filteredTasks.isEmpty {
                    ContentUnavailableView(
                        searchText.isEmpty ? "Активных задач нет" : "Ничего не найдено",
                        systemImage: searchText.isEmpty ? "checkmark.circle" : "magnifyingglass"
                    )
                } else {
                    List(filteredTasks) { task in
                        Button {
                            Task { await choose(task) }
                        } label: {
                            HStack(alignment: .top, spacing: 12) {
                                VStack(alignment: .leading, spacing: 4) {
                                    Text(task.title)
                                        .font(.body)
                                        .fixedSize(horizontal: false, vertical: true)

                                    HStack(spacing: 7) {
                                        if task.isPersonal {
                                            Text("Личное")
                                        } else if task.project.uppercased() == "INBOX" {
                                            Text("Входящие")
                                        } else if !task.project.isEmpty {
                                            Text(task.project)
                                        }

                                        if let deadline = task.deadline {
                                            Text(taskDeadlineText(deadline))
                                                .foregroundStyle(task.overdue ? .red : .secondary)
                                        }
                                    }
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                                }

                                Spacer(minLength: 8)

                                if isCurrent(task) {
                                    Image(systemName: "checkmark.circle.fill")
                                        .foregroundStyle(.tint)
                                        .accessibilityHidden(true)
                                }
                            }
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .disabled(isApplying)
                        .accessibilityLabel(
                            isCurrent(task)
                                ? "\(task.title), выбрано сейчас"
                                : "\(task.title), назначить сейчас"
                        )
                    }
                    .listStyle(.plain)
                }
            }
            .navigationTitle("Что сейчас?")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Готово") {
                        dismiss()
                    }
                }
            }
            .searchable(text: $searchText, prompt: "Найти задачу")
            .task {
                await load()
            }
        }
    }

    private func isCurrent(_ task: TodayTask) -> Bool {
        task.id == currentTaskID || task.isFocused
    }

    @MainActor
    private func load() async {
        guard settings.isConfigured else { return }

        isLoading = true
        errorMessage = nil
        defer { isLoading = false }

        do {
            let client = APIClient(baseURL: settings.normalizedBaseURL, token: settings.token)
            tasks = try await client.loadTasks().tasks
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    @MainActor
    private func choose(_ task: TodayTask) async {
        if isCurrent(task) {
            dismiss()
            return
        }

        guard !isApplying else { return }
        isApplying = true
        errorMessage = nil
        defer { isApplying = false }

        do {
            let client = APIClient(baseURL: settings.normalizedBaseURL, token: settings.token)
            _ = try await client.focusTask(taskID: task.id)
            WidgetCenter.shared.reloadTimelines(ofKind: "AssistantPocketWidget")
            onChanged()
            dismiss()
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}
