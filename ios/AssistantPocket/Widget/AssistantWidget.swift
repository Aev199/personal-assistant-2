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

private struct AssistantWidgetEntry: TimelineEntry {
    let date: Date
    let tasks: [WidgetTask]
    let reminders: [WidgetReminder]
    let error: String?
}

private struct AssistantWidgetProvider: TimelineProvider {
    func placeholder(in context: Context) -> AssistantWidgetEntry {
        AssistantWidgetEntry(
            date: .now,
            tasks: [
                WidgetTask(id: 1, title: "Проверить расчёт", project: "БГР", status: "in_progress", deadline: .now, overdue: false),
                WidgetTask(id: 2, title: "Ответить Иванову", project: "", status: "todo", deadline: nil, overdue: false),
            ],
            reminders: [],
            error: nil
        )
    }

    func getSnapshot(in context: Context, completion: @escaping (AssistantWidgetEntry) -> Void) {
        if context.isPreview {
            completion(placeholder(in: context))
            return
        }

        Task {
            completion(await load())
        }
    }

    func getTimeline(in context: Context, completion: @escaping (Timeline<AssistantWidgetEntry>) -> Void) {
        Task {
            let entry = await load()
            let retry = entry.error == nil ? 15 * 60 : 60
            completion(
                Timeline(
                    entries: [entry],
                    policy: .after(Date().addingTimeInterval(TimeInterval(retry)))
                )
            )
        }
    }

    private func load() async -> AssistantWidgetEntry {
        let server = WidgetSharedSettings.baseURL
        let token = WidgetSharedSettings.token

        guard !server.isEmpty, !token.isEmpty,
              let url = URL(string: server + "/api/v1/companion/today") else {
            return AssistantWidgetEntry(
                date: .now,
                tasks: [],
                reminders: [],
                error: "Откройте Assistant и сохраните настройки"
            )
        }

        do {
            var request = URLRequest(url: url)
            request.timeoutInterval = 8
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
            request.setValue("application/json", forHTTPHeaderField: "Accept")

            let config = URLSessionConfiguration.ephemeral
            config.timeoutIntervalForRequest = 8
            config.timeoutIntervalForResource = 10
            config.waitsForConnectivity = false

            let (data, response) = try await URLSession(configuration: config).data(for: request)
            guard let http = response as? HTTPURLResponse else {
                throw URLError(.badServerResponse)
            }

            if http.statusCode == 401 {
                return AssistantWidgetEntry(
                    date: .now,
                    tasks: [],
                    reminders: [],
                    error: "Неверный код доступа"
                )
            }

            guard 200..<300 ~= http.statusCode else {
                throw URLError(.badServerResponse)
            }

            let decoder = JSONDecoder()
            decoder.dateDecodingStrategy = .iso8601
            let today = try decoder.decode(WidgetTodayResponse.self, from: data)

            return AssistantWidgetEntry(
                date: .now,
                tasks: today.tasks,
                reminders: today.reminders,
                error: nil
            )
        } catch {
            return AssistantWidgetEntry(
                date: .now,
                tasks: [],
                reminders: [],
                error: "Нет связи"
            )
        }
    }
}

private struct AssistantWidgetView: View {
    @Environment(\.widgetFamily) private var family
    let entry: AssistantWidgetEntry

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
            Image(systemName: task.inProgress ? "circle.inset.filled" : "circle")
                .font(.title3)
                .foregroundStyle(.secondary)

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
            Image(systemName: "circle")
                .font(.caption)
                .foregroundStyle(.secondary)

            Text(task.title)
                .font(.caption)
                .lineLimit(1)

            Spacer(minLength: 0)

            if let deadline = task.deadline {
                Text(deadline, format: .dateTime.hour().minute())
                    .font(.caption2)
                    .foregroundStyle(task.overdue ? .red : .secondary)
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
                Text(deadline, format: .dateTime.hour().minute())
                    .foregroundStyle(task.overdue ? .red : .secondary)
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

            if error == "Нет связи" {
                Text("Проверьте VPN и обновите виджет")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            } else if error == "Откройте Assistant и сохраните настройки" {
                Text("Настройка виджета теперь выполняется в приложении")
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
        StaticConfiguration(
            kind: kind,
            provider: AssistantWidgetProvider()
        ) { entry in
            AssistantWidgetView(entry: entry)
        }
        .configurationDisplayName("Assistant — Сейчас")
        .description("Одно главное дело и то, что дальше.")
        .supportedFamilies([.systemMedium, .systemLarge])
        .contentMarginsDisabled()
    }
}

@main
struct AssistantPocketWidgetBundle: WidgetBundle {
    @WidgetBundleBuilder
    var body: some Widget {
        AssistantPocketWidget()
    }
}
