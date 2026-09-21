import AppIntents
import Foundation
import SwiftUI
import WidgetKit

private struct WidgetTask: Decodable, Identifiable {
    let id: Int
    let title: String
    let project: String
    let status: String?
    let deadline: Date?
    let overdue: Bool

    var inProgress: Bool {
        status?.lowercased() == "in_progress"
    }
}

private struct WidgetReminder: Decodable, Identifiable {
    let id: Int
    let text: String
    let at: Date?
}

private struct WidgetTodayResponse: Decodable {
    let tasks: [WidgetTask]
    let reminders: [WidgetReminder]
}

struct AssistantWidgetConfigurationIntent: WidgetConfigurationIntent {
    static var title: LocalizedStringResource = "Assistant"
    static var description = IntentDescription("Подключение виджета к Personal Assistant")

    @Parameter(title: "Адрес Assistant", description: "Например https://assistant.example.com")
    var serverURL: String?

    @Parameter(title: "Код виджета", description: "Отдельный код доступа для виджета")
    var token: String?
}

struct MarkTaskDoneIntent: AppIntent {
    static var title: LocalizedStringResource = "Выполнить задачу"
    static var description = IntentDescription("Закрывает задачу Personal Assistant")

    @Parameter(title: "Task ID")
    var taskID: Int

    @Parameter(title: "Server URL")
    var serverURL: String

    @Parameter(title: "Token")
    var token: String

    init() {}

    init(taskID: Int, serverURL: String, token: String) {
        self.taskID = taskID
        self.serverURL = serverURL
        self.token = token
    }

    func perform() async throws -> some IntentResult {
        let server = normalizedBaseURL(serverURL)
        let cleanToken = token.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !server.isEmpty, !cleanToken.isEmpty,
              let url = URL(string: server + "/api/v1/companion/tasks/\(taskID)/done") else {
            return .result()
        }

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = 10
        request.setValue("Bearer \(cleanToken)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = Data("{}".utf8)

        let (_, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse, 200..<300 ~= http.statusCode else {
            return .result()
        }

        WidgetCenter.shared.reloadAllTimelines()
        return .result()
    }
}

private struct AssistantWidgetEntry: TimelineEntry {
    let date: Date
    let configuration: AssistantWidgetConfigurationIntent
    let tasks: [WidgetTask]
    let reminders: [WidgetReminder]
    let error: String?
}

private struct AssistantWidgetProvider: AppIntentTimelineProvider {
    func placeholder(in context: Context) -> AssistantWidgetEntry {
        AssistantWidgetEntry(
            date: .now,
            configuration: AssistantWidgetConfigurationIntent(),
            tasks: [
                WidgetTask(id: 1, title: "Проверить расчёт", project: "БГР", status: "in_progress", deadline: .now, overdue: false),
                WidgetTask(id: 2, title: "Ответить Иванову", project: "", status: "todo", deadline: nil, overdue: false),
                WidgetTask(id: 3, title: "Подготовить замечания", project: "", status: "todo", deadline: nil, overdue: false),
            ],
            reminders: [],
            error: nil
        )
    }

    func snapshot(for configuration: AssistantWidgetConfigurationIntent, in context: Context) async -> AssistantWidgetEntry {
        if context.isPreview {
            return placeholder(in: context)
        }
        return await load(configuration)
    }

    func timeline(for configuration: AssistantWidgetConfigurationIntent, in context: Context) async -> Timeline<AssistantWidgetEntry> {
        let entry = await load(configuration)
        return Timeline(entries: [entry], policy: .after(Date().addingTimeInterval(15 * 60)))
    }

    private func load(_ configuration: AssistantWidgetConfigurationIntent) async -> AssistantWidgetEntry {
        let server = normalizedBaseURL(configuration.serverURL ?? "")
        let token = (configuration.token ?? "").trimmingCharacters(in: .whitespacesAndNewlines)

        guard !server.isEmpty, !token.isEmpty,
              let url = URL(string: server + "/api/v1/companion/today") else {
            return AssistantWidgetEntry(
                date: .now,
                configuration: configuration,
                tasks: [],
                reminders: [],
                error: "Настройте виджет"
            )
        }

        do {
            var request = URLRequest(url: url)
            request.timeoutInterval = 10
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
            request.setValue("application/json", forHTTPHeaderField: "Accept")

            let (data, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse, 200..<300 ~= http.statusCode else {
                throw URLError(.badServerResponse)
            }

            let decoder = JSONDecoder()
            decoder.dateDecodingStrategy = .iso8601
            let today = try decoder.decode(WidgetTodayResponse.self, from: data)
            return AssistantWidgetEntry(
                date: .now,
                configuration: configuration,
                tasks: today.tasks,
                reminders: today.reminders,
                error: nil
            )
        } catch {
            return AssistantWidgetEntry(
                date: .now,
                configuration: configuration,
                tasks: [],
                reminders: [],
                error: "Нет связи"
            )
        }
    }
}

private func normalizedBaseURL(_ value: String) -> String {
    value.trimmingCharacters(in: CharacterSet(charactersIn: " /\n\t"))
}

private struct AssistantWidgetView: View {
    @Environment(\.widgetFamily) private var family
    let entry: AssistantWidgetEntry

    private var configuredServer: String {
        entry.configuration.serverURL ?? ""
    }

    private var configuredToken: String {
        entry.configuration.token ?? ""
    }

    private var visibleTaskCount: Int {
        family == .systemLarge ? 5 : 3
    }

    var body: some View {
        VStack(alignment: .leading, spacing: family == .systemLarge ? 10 : 7) {
            header

            if let error = entry.error {
                errorState(error)
            } else if let focus = entry.tasks.first {
                focusTask(focus)

                let rest = Array(entry.tasks.dropFirst().prefix(max(0, visibleTaskCount - 1)))
                if !rest.isEmpty {
                    Text("Дальше")
                        .font(.caption2.weight(.semibold))
                        .foregroundStyle(.secondary)

                    ForEach(rest) { task in
                        compactTask(task)
                    }
                }

                if family == .systemLarge, let reminder = entry.reminders.first {
                    compactReminder(reminder)
                }

                Spacer(minLength: 0)
            } else if let reminder = entry.reminders.first {
                focusReminder(reminder)
                Spacer(minLength: 0)
            } else {
                emptyState
            }
        }
        .padding(12)
        .containerBackground(.fill.tertiary, for: .widget)
    }

    private var header: some View {
        HStack(spacing: 8) {
            Text("Сейчас")
                .font(.headline)

            Spacer()

            Link(destination: URL(string: "assistantpocket://capture")!) {
                Image(systemName: "plus.circle.fill")
                    .font(.title3)
            }
            .accessibilityLabel("Запомнить")
        }
    }

    private func focusTask(_ task: WidgetTask) -> some View {
        HStack(alignment: .top, spacing: 9) {
            Button(intent: MarkTaskDoneIntent(
                taskID: task.id,
                serverURL: configuredServer,
                token: configuredToken
            )) {
                Image(systemName: task.inProgress ? "circle.inset.filled" : "circle")
                    .font(.title3)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Выполнено")

            VStack(alignment: .leading, spacing: 3) {
                Text(task.title)
                    .font(.subheadline.weight(.semibold))
                    .lineLimit(family == .systemLarge ? 2 : 1)

                taskMeta(task)
            }

            Spacer(minLength: 0)
        }
    }

    private func compactTask(_ task: WidgetTask) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Button(intent: MarkTaskDoneIntent(
                taskID: task.id,
                serverURL: configuredServer,
                token: configuredToken
            )) {
                Image(systemName: "circle")
                    .font(.caption)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Выполнено")

            Text(task.title)
                .font(.caption)
                .lineLimit(1)

            Spacer(minLength: 0)

            if let deadline = task.deadline {
                if task.overdue {
                    Text(deadline, format: .dateTime.hour().minute())
                        .font(.caption2)
                        .foregroundStyle(.red)
                } else {
                    Text(deadline, format: .dateTime.hour().minute())
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
            }
        }
    }

    @ViewBuilder
    private func taskMeta(_ task: WidgetTask) -> some View {
        HStack(spacing: 5) {
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

            if task.inProgress {
                Text("в работе")
            }
        }
        .font(.caption2)
        .foregroundStyle(.secondary)
    }

    private func focusReminder(_ reminder: WidgetReminder) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Label("Напоминание", systemImage: "bell.fill")
                .font(.caption2.weight(.semibold))
                .foregroundStyle(.secondary)
            Text(reminder.text)
                .font(.subheadline.weight(.semibold))
                .lineLimit(2)
            if let at = reminder.at {
                Text(at, format: .dateTime.hour().minute())
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
        }
    }

    private func compactReminder(_ reminder: WidgetReminder) -> some View {
        HStack(spacing: 7) {
            Image(systemName: "bell")
                .font(.caption2)
                .foregroundStyle(.secondary)
            Text(reminder.text)
                .font(.caption)
                .lineLimit(1)
            Spacer(minLength: 0)
            if let at = reminder.at {
                Text(at, format: .dateTime.hour().minute())
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
        }
    }

    private func errorState(_ error: String) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            Spacer(minLength: 0)
            Text(error)
                .font(.subheadline)
                .foregroundStyle(.secondary)
            if error == "Настройте виджет" {
                Text("Зажмите → Изменить виджет")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }
            Spacer(minLength: 0)
        }
    }

    private var emptyState: some View {
        VStack(alignment: .leading, spacing: 6) {
            Spacer(minLength: 0)
            Text("Свободно")
                .font(.subheadline.weight(.semibold))
            Text("Запишите следующее дело, когда оно появится.")
                .font(.caption)
                .foregroundStyle(.secondary)
            Spacer(minLength: 0)
        }
    }
}

struct AssistantPocketWidget: Widget {
    let kind = "AssistantPocketWidget"

    var body: some WidgetConfiguration {
        AppIntentConfiguration(
            kind: kind,
            intent: AssistantWidgetConfigurationIntent.self,
            provider: AssistantWidgetProvider()
        ) { entry in
            AssistantWidgetView(entry: entry)
        }
        .configurationDisplayName("Assistant — Сейчас")
        .description("Одно главное дело, следующее и быстрый ввод.")
        .supportedFamilies([.systemMedium, .systemLarge])
        .contentMarginsDisabled()
    }
}

@available(iOS 18.0, *)
struct AssistantCaptureControl: ControlWidget {
    let kind = "com.aev199.assistantpocket.capture-control"

    var body: some ControlWidgetConfiguration {
        StaticControlConfiguration(kind: kind) {
            ControlWidgetButton(action: OpenAssistantCaptureIntent()) {
                Label("Быстрый ввод", systemImage: "plus.bubble.fill")
            }
        }
        .displayName("Быстрый ввод")
        .description("Открывает Assistant сразу в поле ввода.")
    }
}

@main
struct AssistantPocketWidgetBundle: WidgetBundle {
    @WidgetBundleBuilder
    var body: some Widget {
        AssistantPocketWidget()
        if #available(iOS 18.0, *) {
            AssistantCaptureControl()
        }
    }
}
