import AppIntents
import Foundation
import SwiftUI
import WidgetKit

private struct WidgetTask: Decodable, Identifiable {
    let id: Int
    let title: String
    let project: String
    let deadline: Date?
    let overdue: Bool
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

    @Parameter(title: "Server URL", description: "Например https://assistant.example.com")
    var serverURL: String?

    @Parameter(title: "Token", description: "COMPANION_WIDGET_TOKEN")
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
                WidgetTask(id: 1, title: "Проверить расчёт", project: "БГР", deadline: .now, overdue: false),
                WidgetTask(id: 2, title: "Подготовить материалы", project: "", deadline: nil, overdue: false),
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

    private var taskLimit: Int {
        family == .systemLarge ? 6 : 3
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Text("Сегодня")
                    .font(.headline)

                Spacer()

                Link(destination: URL(string: "assistantpocket://capture")!) {
                    Image(systemName: "plus.circle.fill")
                        .font(.title3)
                }
                .accessibilityLabel("Добавить")
            }

            if let error = entry.error {
                Spacer()
                Text(error)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                if error == "Настройте виджет" {
                    Text("Зажмите → Изменить виджет")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                }
                Spacer()
            } else if entry.tasks.isEmpty && entry.reminders.isEmpty {
                Spacer()
                Text("Ничего срочного")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                Spacer()
            } else {
                ForEach(Array(entry.tasks.prefix(taskLimit))) { task in
                    taskRow(task)
                }

                if family == .systemLarge {
                    ForEach(Array(entry.reminders.prefix(2))) { reminder in
                        HStack(spacing: 8) {
                            Image(systemName: "bell")
                                .font(.caption)
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
                }

                Spacer(minLength: 0)
            }
        }
        .padding(12)
        .containerBackground(.fill.tertiary, for: .widget)
    }

    @ViewBuilder
    private func taskRow(_ task: WidgetTask) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Button(intent: MarkTaskDoneIntent(
                taskID: task.id,
                serverURL: configuredServer,
                token: configuredToken
            )) {
                Image(systemName: "circle")
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Выполнено")

            VStack(alignment: .leading, spacing: 1) {
                Text(task.title)
                    .font(.caption)
                    .lineLimit(1)

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
                }
                .font(.caption2)
                .foregroundStyle(.secondary)
            }

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
        .configurationDisplayName("Assistant — Сегодня")
        .description("Дела на сегодня, Quick Done и быстрый захват.")
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
