import SwiftUI
import WidgetKit

struct TaskEditView: View {
    @EnvironmentObject private var settings: AppSettings
    @Environment(\.dismiss) private var dismiss

    let task: TodayTask
    let onSaved: () -> Void

    @State private var title: String
    @State private var projectCode: String
    @State private var hasDeadline: Bool
    @State private var deadline: Date
    @State private var projects: [AssistantProject] = []
    @State private var isSaving = false
    @State private var errorMessage: String?

    init(task: TodayTask, onSaved: @escaping () -> Void) {
        self.task = task
        self.onSaved = onSaved
        _title = State(initialValue: task.title)
        _projectCode = State(initialValue: task.project.isEmpty ? "INBOX" : task.project)
        _hasDeadline = State(initialValue: task.deadline != nil)

        let tomorrow = Calendar.current.date(byAdding: .day, value: 1, to: .now) ?? .now
        let defaultDeadline =
            Calendar.current.date(bySettingHour: 10, minute: 0, second: 0, of: tomorrow)
            ?? tomorrow
        _deadline = State(initialValue: task.deadline ?? defaultDeadline)
    }

    var body: some View {
        NavigationStack {
            Form {
                Section("Задача") {
                    TextField("Что нужно сделать", text: $title, axis: .vertical)
                        .lineLimit(1...4)
                }

                if !task.isPersonal {
                    Section("Проект") {
                        if projects.isEmpty {
                            HStack {
                                Text(projectCode.uppercased() == "INBOX" ? "Входящие" : projectCode)
                                Spacer()
                                if errorMessage == nil {
                                    ProgressView()
                                        .controlSize(.small)
                                }
                            }
                        } else {
                            Picker("Проект", selection: $projectCode) {
                                if !projects.contains(where: {
                                    $0.code.caseInsensitiveCompare(projectCode) == .orderedSame
                                }) {
                                    Text(
                                        projectCode.uppercased() == "INBOX"
                                            ? "Входящие"
                                            : "Текущий · \(projectCode)"
                                    )
                                    .tag(projectCode)
                                }

                                ForEach(projects) { project in
                                    Text(project.displayName)
                                        .tag(project.code)
                                }
                            }
                            .labelsHidden()
                        }
                    }
                }

                Section("Срок") {
                    Toggle("Указать срок", isOn: $hasDeadline)

                    if hasDeadline {
                        DatePicker(
                            "Дата и время",
                            selection: $deadline,
                            displayedComponents: [.date, .hourAndMinute]
                        )
                    }
                }

                if let errorMessage {
                    Section {
                        Text(errorMessage)
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                    }
                }
            }
            .navigationTitle("Задача")
            .navigationBarTitleDisplayMode(.inline)
            .task {
                if !task.isPersonal {
                    await loadProjects()
                }
            }
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Отмена") {
                        dismiss()
                    }
                }

                ToolbarItem(placement: .confirmationAction) {
                    Button("Сохранить") {
                        Task { await save() }
                    }
                    .disabled(
                        title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                        || (!task.isPersonal && projectCode.isEmpty)
                        || isSaving
                    )
                }
            }
        }
    }

    @MainActor
    private func loadProjects() async {
        errorMessage = nil
        do {
            let client = APIClient(baseURL: settings.normalizedBaseURL, token: settings.token)
            projects = try await client.loadProjects().projects
        } catch {
            errorMessage = "Не удалось загрузить проекты."
        }
    }

    @MainActor
    private func save() async {
        let cleanTitle = title.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleanTitle.isEmpty else { return }
        guard task.isPersonal || !projectCode.isEmpty else { return }

        isSaving = true
        errorMessage = nil
        defer { isSaving = false }

        do {
            let client = APIClient(baseURL: settings.normalizedBaseURL, token: settings.token)
            let originalProjectCode = task.project.isEmpty ? "INBOX" : task.project
            let projectChanged =
                !task.isPersonal
                && projectCode.caseInsensitiveCompare(originalProjectCode) != .orderedSame

            _ = try await client.updateTask(
                taskID: task.id,
                title: cleanTitle,
                projectCode: projectChanged ? projectCode : nil,
                deadline: hasDeadline ? deadline : nil
            )
            WidgetCenter.shared.reloadTimelines(ofKind: "AssistantPocketWidget")
            onSaved()
            dismiss()
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}
