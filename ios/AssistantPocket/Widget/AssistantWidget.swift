import AppIntents
import Foundation
import SwiftUI
import WidgetKit

private struct WidgetTask: Codable, Identifiable {
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

private struct WidgetReminder: Codable, Identifiable {
    let id: Int
    let text: String
    let at: Date?
}

private struct WidgetEvent: Codable, Identifiable {
    let id: String
    let title: String
    let start: Date
    let end: Date
    let kind: String
}

private struct WidgetTodayResponse: Codable {
    var tasks: [WidgetTask]
    var reminders: [WidgetReminder]
    var events: [WidgetEvent]?
}

private enum WidgetCodec {
    static func decodeToday(_ data: Data) -> WidgetTodayResponse? {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        return try? decoder.decode(WidgetTodayResponse.self, from: data)
    }

    static func encodeToday(_ value: WidgetTodayResponse) -> Data? {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        return try? encoder.encode(value)
    }

    static func cachedEntry() -> AssistantWidgetEntry? {
        guard let data = WidgetSharedSettings.cachedTodayData,
              let today = decodeToday(data) else {
            return nil
        }
        return AssistantWidgetEntry(
            date: .now,
            tasks: today.tasks,
            reminders: today.reminders,
            events: today.events ?? [],
            error: nil
        )
    }

    static func cacheWithoutTask(_ taskID: Int) -> Data? {
        guard let data = WidgetSharedSettings.cachedTodayData,
              var today = decodeToday(data) else {
            return nil
        }
        today.tasks.removeAll { $0.id == taskID }
        return encodeToday(today)
    }
}

private enum WidgetNetwork {
    static func loadToday() async throws -> (Data, HTTPURLResponse) {
        try await send(
            path: "/api/v1/today",
            legacyPath: "/api/v1/companion/today"
        )
    }

    static func markDone(taskID: Int) async throws {
        let (_, response) = try await send(
            path: "/api/v1/tasks/\(taskID)/done",
            legacyPath: "/api/v1/companion/tasks/\(taskID)/done",
            method: "POST",
            body: Data("{}".utf8)
        )

        guard 200..<300 ~= response.statusCode else {
            throw URLError(.badServerResponse)
        }
    }

    private static func send(
        path: String,
        legacyPath: String? = nil,
        method: String = "GET",
        body: Data? = nil
    ) async throws -> (Data, HTTPURLResponse) {
        let server = WidgetSharedSettings.baseURL
        let token = WidgetSharedSettings.token
        guard !server.isEmpty, !token.isEmpty else {
            throw URLError(.userAuthenticationRequired)
        }

        func makeRequest(_ path: String) throws -> URLRequest {
            guard let url = URL(string: server + path) else {
                throw URLError(.badURL)
            }
            var request = URLRequest(url: url)
            request.httpMethod = method
            request.timeoutInterval = 6
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
            request.setValue("application/json", forHTTPHeaderField: "Accept")
            if let body {
                request.setValue("application/json", forHTTPHeaderField: "Content-Type")
                request.httpBody = body
            }
            return request
        }

        let primary = try makeRequest(path)
        let (primaryData, primaryResponse) = try await URLSession.shared.data(for: primary)
        guard let primaryHTTP = primaryResponse as? HTTPURLResponse else {
            throw URLError(.badServerResponse)
        }

        if primaryHTTP.statusCode == 404, let legacyPath {
            let legacy = try makeRequest(legacyPath)
            let (legacyData, legacyResponse) = try await URLSession.shared.data(for: legacy)
            guard let legacyHTTP = legacyResponse as? HTTPURLResponse else {
                throw URLError(.badServerResponse)
            }
            return (legacyData, legacyHTTP)
        }

        return (primaryData, primaryHTTP)
    }
}

struct MarkTaskDoneIntent: AppIntent {
    static var title: LocalizedStringResource = "Выполнить задачу"
    static var description = IntentDescription("Закрывает задачу Personal Assistant.")
    static var openAppWhenRun = false

    @Parameter(title: "Task ID")
    var taskID: Int

    init() {}

    init(taskID: Int) {
        self.taskID = taskID
    }

    func perform() async throws -> some IntentResult {
        let originalCache = WidgetSharedSettings.cachedTodayData

        if let optimisticCache = WidgetCodec.cacheWithoutTask(taskID) {
            WidgetSharedSettings.writeCachedTodayData(optimisticCache)
            WidgetSharedSettings.requestCachedTodayOnce()
            WidgetCenter.shared.reloadTimelines(ofKind: "AssistantPocketWidget")
        }

        do {
            try await WidgetNetwork.markDone(taskID: taskID)
            return .result()
        } catch {
            if let originalCache {
                WidgetSharedSettings.writeCachedTodayData(originalCache)
            } else {
                WidgetSharedSettings.clearCachedTodayData()
            }
            WidgetSharedSettings.requestCachedTodayOnce()
            WidgetCenter.shared.reloadTimelines(ofKind: "AssistantPocketWidget")
            throw error
        }
    }
}

struct RefreshAssistantWidgetIntent: AppIntent {
    static var title: LocalizedStringResource = "Обновить Assistant"
    static var description = IntentDescription("Запрашивает свежие данные для виджета.")
    static var openAppWhenRun = false

    init() {}

    func perform() async throws -> some IntentResult {
        WidgetCenter.shared.reloadTimelines(ofKind: "AssistantPocketWidget")
        return .result()
    }
}

private struct AssistantWidgetEntry: TimelineEntry {
    let date: Date
    let tasks: [WidgetTask]
    let reminders: [WidgetReminder]
    let events: [WidgetEvent]
    let error: String?
}

private struct AssistantWidgetProvider: TimelineProvider {
    func placeholder(in context: Context) -> AssistantWidgetEntry {
        AssistantWidgetEntry(
            date: .now,
            tasks: [
                WidgetTask(id: 1, title: "Проверить расчёт", project: "БГР", status: "in_progress", deadline: .now, overdue: false),
                WidgetTask(id: 2, title: "Ответить Иванову", project: "", status: "todo", deadline: nil, overdue: false),
                WidgetTask(id: 3, title: "Подготовить замечания", project: "", status: "todo", deadline: nil, overdue: false),
            ],
            reminders: [],
            events: [],
            error: nil
        )
    }

    func getSnapshot(in context: Context, completion: @escaping (AssistantWidgetEntry) -> Void) {
        if context.isPreview {
            completion(placeholder(in: context))
            return
        }

        if let cached = WidgetCodec.cachedEntry() {
            completion(cached)
            return
        }

        Task {
            completion(await load())
        }
    }

    func getTimeline(in context: Context, completion: @escaping (Timeline<AssistantWidgetEntry>) -> Void) {
        Task {
            let entry: AssistantWidgetEntry

            if WidgetSharedSettings.consumeCachedTodayOnce(),
               let cached = WidgetCodec.cachedEntry() {
                entry = cached
            } else {
                entry = await load()
            }

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

        guard !server.isEmpty, !token.isEmpty else {
            return AssistantWidgetEntry(
                date: .now,
                tasks: [],
                reminders: [],
                events: [],
                error: "Откройте Assistant и сохраните настройки"
            )
        }

        do {
            let (data, http) = try await WidgetNetwork.loadToday()

            if http.statusCode == 401 {
                return AssistantWidgetEntry(
                    date: .now,
                    tasks: [],
                    reminders: [],
                    events: [],
                    error: "Неверный код доступа"
                )
            }

            guard 200..<300 ~= http.statusCode else {
                throw URLError(.badServerResponse)
            }

            guard let today = WidgetCodec.decodeToday(data) else {
                throw URLError(.cannotDecodeContentData)
            }
            WidgetSharedSettings.writeCachedTodayData(data)

            return AssistantWidgetEntry(
                date: .now,
                tasks: today.tasks,
                reminders: today.reminders,
                events: today.events ?? [],
                error: nil
            )
        } catch {
            if let cached = WidgetCodec.cachedEntry() {
                return cached
            }
            return AssistantWidgetEntry(
                date: .now,
                tasks: [],
                reminders: [],
                events: [],
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

    private var activeEvent: WidgetEvent? {
        entry.events.first { $0.start <= entry.date && $0.end > entry.date }
    }

    private var dueSoonReminder: WidgetReminder? {
        let cutoff = entry.date.addingTimeInterval(15 * 60)
        return entry.reminders.first(where: { reminder in
            guard let at = reminder.at else { return false }
            return at <= cutoff
        })
    }

    private var upcomingEvent: WidgetEvent? {
        guard activeEvent == nil, dueSoonReminder == nil else { return nil }
        let cutoff = entry.date.addingTimeInterval(15 * 60)
        return entry.events.first { $0.end > entry.date && $0.start <= cutoff }
    }

    private var focusEvent: WidgetEvent? {
        if let activeEvent { return activeEvent }
        if let upcomingEvent { return upcomingEvent }
        if entry.tasks.isEmpty && entry.reminders.isEmpty { return entry.events.first }
        return nil
    }

    private var focusReminder: WidgetReminder? {
        guard activeEvent == nil else { return nil }
        if let dueSoonReminder { return dueSoonReminder }
        return focusEvent == nil && entry.tasks.isEmpty ? entry.reminders.first : nil
    }

    var body: some View {
        VStack(alignment: .leading, spacing: family == .systemLarge ? 10 : 7) {
            header

            if let error = entry.error {
                errorState(error)
            } else if let event = focusEvent {
                focusEventView(event)

                let rest = Array(entry.tasks.prefix(max(0, visibleTaskCount - 1)))
                if !rest.isEmpty {
                    Text("Дальше")
                        .font(.caption2.weight(.semibold))
                        .foregroundStyle(.secondary)

                    ForEach(rest) { task in
                        compactTask(task)
                    }
                }

                Spacer(minLength: 0)
            } else if let reminder = focusReminder {
                focusReminderView(reminder)

                let rest = Array(entry.tasks.prefix(max(0, visibleTaskCount - 1)))
                if !rest.isEmpty {
                    Text("Дальше")
                        .font(.caption2.weight(.semibold))
                        .foregroundStyle(.secondary)

                    ForEach(rest) { task in
                        compactTask(task)
                    }
                }

                Spacer(minLength: 0)
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

                if family == .systemLarge, let event = entry.events.first {
                    compactEvent(event)
                } else if family == .systemLarge, let reminder = entry.reminders.first {
                    compactReminder(reminder)
                }

                Spacer(minLength: 0)
            } else if let reminder = entry.reminders.first {
                focusReminderView(reminder)
                Spacer(minLength: 0)
            } else if let event = entry.events.first {
                focusEventView(event)
                Spacer(minLength: 0)
            } else {
                emptyState
            }
        }
        .padding(12)
        .containerBackground(.fill.tertiary, for: .widget)
    }

    private var header: some View {
        HStack(spacing: 9) {
            Text("Сейчас")
                .font(.headline)

            Spacer()

            Button(intent: RefreshAssistantWidgetIntent()) {
                Image(systemName: "arrow.clockwise")
                    .font(.subheadline.weight(.semibold))
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Обновить")

            Link(destination: URL(string: "assistantpocket://capture")!) {
                Image(systemName: "plus.circle.fill")
                    .font(.title3)
            }
            .accessibilityLabel("Запомнить")
        }
    }

    private func focusTask(_ task: WidgetTask) -> some View {
        HStack(alignment: .top, spacing: 9) {
            Button(intent: MarkTaskDoneIntent(taskID: task.id)) {
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
            Button(intent: MarkTaskDoneIntent(taskID: task.id)) {
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

    private func focusEventView(_ event: WidgetEvent) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Label("Календарь", systemImage: "calendar")
                .font(.caption2.weight(.semibold))
                .foregroundStyle(.secondary)

            Text(event.title)
                .font(.subheadline.weight(.semibold))
                .lineLimit(2)

            Text(event.start, format: .dateTime.hour().minute())
                + Text("–")
                + Text(event.end, format: .dateTime.hour().minute())
        }
    }

    private func compactEvent(_ event: WidgetEvent) -> some View {
        HStack(spacing: 7) {
            Image(systemName: "calendar")
                .font(.caption2)
                .foregroundStyle(.secondary)

            Text(event.title)
                .font(.caption)
                .lineLimit(1)

            Spacer(minLength: 0)

            Text(event.start, format: .dateTime.hour().minute())
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
    }

    private func focusReminderView(_ reminder: WidgetReminder) -> some View {
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
                Text("Проверьте VPN и нажмите ↻")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            } else if error == "Откройте Assistant и сохраните настройки" {
                Text("Настройка виджета выполняется в приложении")
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
        .description("Главное дело, следующие задачи и быстрые действия.")
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
