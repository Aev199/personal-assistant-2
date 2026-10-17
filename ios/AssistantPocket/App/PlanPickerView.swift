import SwiftUI
import WidgetKit

/// Choose today's tasks: one tap adds a task to «На сегодня», a second tap removes it.
struct PlanPickerView: View {
    @Environment(\.dismiss) private var dismiss
    @EnvironmentObject private var settings: AppSettings

    let onChanged: () -> Void

    @State private var tasks: [TodayTask] = []
    @State private var plannedIDs: Set<Int> = []
    /// Order is fixed when the list opens, so rows do not jump under the finger.
    @State private var initiallyPlannedIDs: Set<Int> = []
    @State private var searchText = ""
    @State private var isLoading = false
    @State private var applyingID: Int?
    @State private var errorMessage: String?

    private var orderedTasks: [TodayTask] {
        let query = searchText.trimmingCharacters(in: .whitespacesAndNewlines)
        let matching = query.isEmpty ? tasks : tasks.filter { task in
            task.title.localizedCaseInsensitiveContains(query)
                || task.project.localizedCaseInsensitiveContains(query)
                || task.assignee.localizedCaseInsensitiveContains(query)
        }
        return matching.filter { initiallyPlannedIDs.contains($0.id) }
            + matching.filter { !initiallyPlannedIDs.contains($0.id) }
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
                } else if orderedTasks.isEmpty {
                    ContentUnavailableView(
                        searchText.isEmpty ? "Активных задач нет" : "Ничего не найдено",
                        systemImage: searchText.isEmpty ? "checkmark.circle" : "magnifyingglass"
                    )
                } else {
                    List(orderedTasks) { task in
                        Button {
                            Task { await toggle(task) }
                        } label: {
                            row(task)
                        }
                        .buttonStyle(.plain)
                        .disabled(applyingID != nil)
                        .accessibilityLabel(
                            plannedIDs.contains(task.id)
                                ? "\(task.title), на сегодня. Убрать"
                                : "\(task.title). Добавить на сегодня"
                        )
                    }
                    .listStyle(.plain)
                }
            }
            .navigationTitle("На сегодня")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Готово") {
                        dismiss()
                    }
                }
            }
            .searchable(text: $searchText, prompt: "Найти задачу")
            .alert("Не удалось изменить план", isPresented: actionErrorPresented) {
                Button("OK", role: .cancel) {}
            } message: {
                Text(errorMessage ?? "")
            }
            .task {
                await load()
            }
        }
    }

    private func row(_ task: TodayTask) -> some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: plannedIDs.contains(task.id) ? "checkmark.circle.fill" : "circle")
                .font(.title3)
                .foregroundStyle(plannedIDs.contains(task.id) ? AnyShapeStyle(.tint) : AnyShapeStyle(.secondary))
                .padding(.top, 1)

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

            if applyingID == task.id {
                ProgressView()
                    .controlSize(.small)
            }
        }
        .contentShape(Rectangle())
        .padding(.vertical, 3)
    }

    private var actionErrorPresented: Binding<Bool> {
        Binding(
            get: { errorMessage != nil && !tasks.isEmpty },
            set: { isPresented in
                if !isPresented {
                    errorMessage = nil
                }
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
            tasks = try await client.loadTasks().tasks
            plannedIDs = Set(tasks.filter(\.isInTodayPlan).map(\.id))
            initiallyPlannedIDs = plannedIDs
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    @MainActor
    private func toggle(_ task: TodayTask) async {
        guard applyingID == nil else { return }
        let planned = !plannedIDs.contains(task.id)
        applyingID = task.id
        errorMessage = nil
        defer { applyingID = nil }

        do {
            let client = APIClient(baseURL: settings.normalizedBaseURL, token: settings.token)
            if planned {
                _ = try await client.planTask(taskID: task.id)
                plannedIDs.insert(task.id)
            } else {
                _ = try await client.unplanTask(taskID: task.id)
                plannedIDs.remove(task.id)
            }
            WidgetCenter.shared.reloadTimelines(ofKind: "AssistantPocketWidget")
            onChanged()
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}
