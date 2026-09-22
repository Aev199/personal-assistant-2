import SwiftUI
import WidgetKit

struct AllTasksView: View {
    @EnvironmentObject private var settings: AppSettings

    let onChanged: () -> Void

    @State private var tasks: [TodayTask] = []
    @State private var searchText = ""
    @State private var isLoading = false
    @State private var errorMessage: String?
    @State private var editingTask: TodayTask?

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
                List {
                    ForEach(filteredTasks) { task in
                        taskRow(task)
                            .contentShape(Rectangle())
                            .onTapGesture {
                                editingTask = task
                            }
                            .swipeActions(edge: .leading, allowsFullSwipe: true) {
                                if !task.isFocused {
                                    Button {
                                        Task { await focus(task) }
                                    } label: {
                                        Label("Сейчас", systemImage: "play.fill")
                                    }
                                }
                            }
                    }
                }
                .listStyle(.plain)
                .refreshable {
                    await load()
                }
            }
        }
        .navigationTitle("Задачи")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                NavigationLink {
                    IdeasView {
                        Task {
                            await load()
                            onChanged()
                        }
                    }
                    .environmentObject(settings)
                } label: {
                    Image(systemName: "lightbulb")
                }
                .accessibilityLabel("Идеи")
            }
        }
        .searchable(text: $searchText, prompt: "Найти задачу")
        .task {
            await load()
        }
        .sheet(item: $editingTask) { task in
            TaskEditView(task: task) {
                Task {
                    await load()
                    onChanged()
                }
            }
            .environmentObject(settings)
        }
    }

    private func taskRow(_ task: TodayTask) -> some View {
        HStack(alignment: .top, spacing: 12) {
            Button {
                Task { await complete(task) }
            } label: {
                Image(systemName: task.inProgress ? "circle.inset.filled" : "circle")
                    .font(.title3)
                    .padding(.top, 2)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Выполнено")

            VStack(alignment: .leading, spacing: 5) {
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
                        Text(deadline, format: .dateTime.day().month().hour().minute())
                            .foregroundStyle(task.overdue ? .red : .secondary)
                    }

                    if task.isFocused {
                        Text("сейчас")
                    } else if task.inProgress {
                        Text("в работе")
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
        .padding(.vertical, 4)
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
        } catch let APIClientError.http(code, _) where code == 404 {
            errorMessage = "Обновите backend Assistant до версии с полным API."
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    @MainActor
    private func focus(_ task: TodayTask) async {
        errorMessage = nil
        do {
            let client = APIClient(baseURL: settings.normalizedBaseURL, token: settings.token)
            _ = try await client.focusTask(taskID: task.id)
            await load()
            WidgetCenter.shared.reloadTimelines(ofKind: "AssistantPocketWidget")
            onChanged()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    @MainActor
    private func complete(_ task: TodayTask) async {
        guard let index = tasks.firstIndex(where: { $0.id == task.id }) else { return }
        let original = tasks.remove(at: index)
        errorMessage = nil

        do {
            let client = APIClient(baseURL: settings.normalizedBaseURL, token: settings.token)
            _ = try await client.markDone(taskID: task.id)
            WidgetCenter.shared.reloadTimelines(ofKind: "AssistantPocketWidget")
            onChanged()
        } catch {
            tasks.insert(original, at: min(index, tasks.count))
            errorMessage = error.localizedDescription
        }
    }
}
