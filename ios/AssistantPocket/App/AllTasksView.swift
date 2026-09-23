import SwiftUI
import WidgetKit

private enum TaskScope: String, CaseIterable, Identifiable {
    case work = "Рабочие"
    case personal = "Личные"

    var id: Self { self }

    var emptyTitle: String {
        switch self {
        case .work: return "Рабочих задач нет"
        case .personal: return "Личных задач нет"
        }
    }
}

struct AllTasksView: View {
    @EnvironmentObject private var settings: AppSettings

    let refreshToken: Int
    let onChanged: () -> Void

    @State private var tasks: [TodayTask] = []
    @State private var searchText = ""
    @State private var scope: TaskScope = .work
    @State private var isLoading = false
    @State private var errorMessage: String?
    @State private var editingTask: TodayTask?

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
            } else {
                VStack(spacing: 0) {
                    Picker("Тип задач", selection: $scope) {
                        ForEach(TaskScope.allCases) { item in
                            Text(item.rawValue).tag(item)
                        }
                    }
                    .pickerStyle(.segmented)
                    .padding(.horizontal, 16)
                    .padding(.top, 8)
                    .padding(.bottom, 6)

                    TabView(selection: $scope) {
                        taskPage(.work)
                            .tag(TaskScope.work)

                        taskPage(.personal)
                            .tag(TaskScope.personal)
                    }
                    .tabViewStyle(.page(indexDisplayMode: .never))
                }
            }
        }
        .navigationTitle("Задачи")
        .navigationBarTitleDisplayMode(.large)
        .toolbar {
            ToolbarItemGroup(placement: .topBarTrailing) {
                Link(destination: URL(string: "assistantpocket://capture?mode=voice")!) {
                    Image(systemName: "mic")
                }
                .accessibilityLabel("Записать голосом")

                Link(destination: URL(string: "assistantpocket://capture")!) {
                    Image(systemName: "plus")
                }
                .accessibilityLabel("Запомнить")
            }
        }
        .searchable(text: $searchText, prompt: "Найти задачу")
        .task {
            await load()
        }
        .onChange(of: refreshToken) { _, _ in
            Task { await load() }
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

    @ViewBuilder
    private func taskPage(_ pageScope: TaskScope) -> some View {
        let visible = filteredTasks(for: pageScope)

        if visible.isEmpty {
            ContentUnavailableView(
                searchText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                    ? pageScope.emptyTitle
                    : "Ничего не найдено",
                systemImage: searchText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                    ? "checkmark.circle"
                    : "magnifyingglass"
            )
        } else {
            List {
                ForEach(visible) { task in
                    taskRow(task)
                        .contentShape(Rectangle())
                        .onTapGesture {
                            editingTask = task
                        }
                        .swipeActions(edge: .leading, allowsFullSwipe: false) {
                            if !task.isFocused {
                                Button {
                                    Task { await focus(task) }
                                } label: {
                                    Label("Сейчас", systemImage: "scope")
                                }
                            }
                        }
                        .swipeActions(edge: .trailing, allowsFullSwipe: true) {
                            Button {
                                Task { await complete(task) }
                            } label: {
                                Label("Готово", systemImage: "checkmark")
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

    private func filteredTasks(for pageScope: TaskScope) -> [TodayTask] {
        let scoped = tasks.filter { task in
            switch pageScope {
            case .work:
                !task.isPersonal
            case .personal:
                task.isPersonal
            }
        }

        let query = searchText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !query.isEmpty else { return scoped }

        return scoped.filter { task in
            task.title.localizedCaseInsensitiveContains(query)
                || task.project.localizedCaseInsensitiveContains(query)
                || task.assignee.localizedCaseInsensitiveContains(query)
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
                        Text(taskDeadlineText(deadline))
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

            Spacer(minLength: 8)
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
