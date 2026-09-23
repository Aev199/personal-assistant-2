import AppIntents
import Foundation
import SwiftUI
import WidgetKit

private struct WidgetTask: Codable, Identifiable {
    let id: Int
    let title: String
    let project: String
    var status: String?
    let deadline: Date?
    let overdue: Bool
    var focused: Bool? = nil
    var focusedSince: Date? = nil

    enum CodingKeys: String, CodingKey {
        case id, title, project, status, deadline, overdue, focused
        case focusedSince = "focused_since"
    }

    var inProgress: Bool {
        status?.lowercased() == "in_progress"
    }

    var isFocused: Bool {
        focused == true
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
    var calendarUnavailable: Bool?
    var calendarPending: Bool?

    enum CodingKeys: String, CodingKey {
        case tasks, reminders, events
        case calendarUnavailable = "calendar_unavailable"
        case calendarPending = "calendar_pending"
    }
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

    static func cachedEntry(stale: Bool = false) -> AssistantWidgetEntry? {
        guard let data = WidgetSharedSettings.cachedTodayData,
              let today = decodeToday(data) else {
            return nil
        }
        return AssistantWidgetEntry(
            date: .now,
            tasks: today.tasks,
            reminders: today.reminders,
            events: today.events ?? [],
            calendarUnavailable: today.calendarUnavailable ?? false,
            calendarPending: today.calendarPending ?? false,
            stale: stale,
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

    static func cacheFocusedTask(_ taskID: Int) -> Data? {
        guard let data = WidgetSharedSettings.cachedTodayData,
              var today = decodeToday(data) else {
            return nil
        }
        for index in today.tasks.indices {
            let isTarget = today.tasks[index].id == taskID
            today.tasks[index].focused = isTarget
            if isTarget {
                today.tasks[index].status = "in_progress"
                if today.tasks[index].focusedSince == nil {
                    today.tasks[index].focusedSince = Date()
                }
            } else {
                today.tasks[index].focusedSince = nil
            }
        }
        return encodeToday(today)
    }

    static func cacheWithoutEvent(_ eventID: String) -> Data? {
        guard let data = WidgetSharedSettings.cachedTodayData,
              var today = decodeToday(data) else {
            return nil
        }
        var events = today.events ?? []
        events.removeAll { $0.id == eventID }
        today.events = events
        return encodeToday(today)
    }

    static func cacheWithoutReminder(_ reminderID: Int) -> Data? {
        guard let data = WidgetSharedSettings.cachedTodayData,
              var today = decodeToday(data) else {
            return nil
        }
        today.reminders.removeAll { $0.id == reminderID }
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

    static func focusTask(taskID: Int) async throws {
        let (_, response) = try await send(
            path: "/api/v1/tasks/\(taskID)/focus",
            method: "POST",
            body: Data("{}".utf8)
        )

        guard 200..<300 ~= response.statusCode else {
            throw URLError(.badServerResponse)
        }
    }

    static func dismissEvent(eventID: String, until: Date) async throws {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let body = try JSONSerialization.data(
            withJSONObject: [
                "event_id": eventID,
                "until": formatter.string(from: until),
            ]
        )
        let (_, response) = try await send(
            path: "/api/v1/attention/dismiss-event",
            method: "POST",
            body: body
        )

        guard 200..<300 ~= response.statusCode else {
            throw URLError(.badServerResponse)
        }
    }

    static func snoozeReminder(reminderID: Int) async throws {
        let body = try JSONSerialization.data(withJSONObject: ["minutes": 15])
        let (_, response) = try await send(
            path: "/api/v1/reminders/\(reminderID)/snooze",
            method: "POST",
            body: body
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

struct FocusTaskIntent: AppIntent {
    static var title: LocalizedStringResource = "Сделать текущей"
    static var description = IntentDescription("Назначает задачу текущим фокусом Personal Assistant.")
    static var openAppWhenRun = false

    @Parameter(title: "Task ID")
    var taskID: Int

    init() {}

    init(taskID: Int) {
        self.taskID = taskID
    }

    func perform() async throws -> some IntentResult {
        let originalCache = WidgetSharedSettings.cachedTodayData

        if let optimisticCache = WidgetCodec.cacheFocusedTask(taskID) {
            WidgetSharedSettings.writeCachedTodayData(optimisticCache)
            WidgetSharedSettings.requestCachedTodayOnce()
            WidgetCenter.shared.reloadTimelines(ofKind: "AssistantPocketWidget")
        }

        do {
            try await WidgetNetwork.focusTask(taskID: taskID)
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

struct DismissCalendarEventIntent: AppIntent {
    static var title: LocalizedStringResource = "Скрыть встречу"
    static var description = IntentDescription("Убирает встречу из внимания до её окончания.")
    static var openAppWhenRun = false

    @Parameter(title: "Event ID")
    var eventID: String

    @Parameter(title: "Event end")
    var eventEnd: Date

    init() {}

    init(eventID: String, eventEnd: Date) {
        self.eventID = eventID
        self.eventEnd = eventEnd
    }

    func perform() async throws -> some IntentResult {
        let originalCache = WidgetSharedSettings.cachedTodayData

        if let optimisticCache = WidgetCodec.cacheWithoutEvent(eventID) {
            WidgetSharedSettings.writeCachedTodayData(optimisticCache)
            WidgetSharedSettings.requestCachedTodayOnce()
            WidgetCenter.shared.reloadTimelines(ofKind: "AssistantPocketWidget")
        }

        do {
            try await WidgetNetwork.dismissEvent(eventID: eventID, until: eventEnd)
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

struct SnoozeReminderIntent: AppIntent {
    static var title: LocalizedStringResource = "Отложить напоминание"
    static var description = IntentDescription("Откладывает напоминание на 15 минут.")
    static var openAppWhenRun = false

    @Parameter(title: "Reminder ID")
    var reminderID: Int

    init() {}

    init(reminderID: Int) {
        self.reminderID = reminderID
    }

    func perform() async throws -> some IntentResult {
        let originalCache = WidgetSharedSettings.cachedTodayData

        if let optimisticCache = WidgetCodec.cacheWithoutReminder(reminderID) {
            WidgetSharedSettings.writeCachedTodayData(optimisticCache)
            WidgetSharedSettings.requestCachedTodayOnce()
            WidgetCenter.shared.reloadTimelines(ofKind: "AssistantPocketWidget")
        }

        do {
            try await WidgetNetwork.snoozeReminder(reminderID: reminderID)
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
    let calendarUnavailable: Bool
    let calendarPending: Bool
    let stale: Bool
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
            calendarUnavailable: false,
            calendarPending: false,
            stale: false,
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

            let nextRefresh: Date
            if entry.error != nil || entry.calendarPending || entry.stale {
                nextRefresh = Date().addingTimeInterval(60)
            } else {
                nextRefresh = nextRefreshDate(for: entry)
            }

            completion(
                Timeline(
                    entries: [entry],
                    policy: .after(nextRefresh)
                )
            )
        }
    }

    private func nextRefreshDate(for entry: AssistantWidgetEntry) -> Date {
        let now = Date()
        var candidates: [Date] = [now.addingTimeInterval(15 * 60)]

        for reminder in entry.reminders {
            guard let at = reminder.at else { continue }

            if at <= now {
                candidates.append(now.addingTimeInterval(60))
            } else {
                candidates.append(at)
            }
        }

        for event in entry.events where event.end > now {
            let attentionStart = event.start.addingTimeInterval(-15 * 60)
            if attentionStart > now {
                candidates.append(attentionStart)
            }
            if event.start > now {
                candidates.append(event.start)
            }
            candidates.append(event.end.addingTimeInterval(5))
        }

        return candidates.min() ?? now.addingTimeInterval(15 * 60)
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
                calendarUnavailable: false,
                calendarPending: false,
                stale: false,
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
                    calendarUnavailable: false,
                    calendarPending: false,
                    stale: false,
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
                calendarUnavailable: today.calendarUnavailable ?? false,
                calendarPending: today.calendarPending ?? false,
                stale: false,
                error: nil
            )
        } catch {
            if let cached = WidgetCodec.cachedEntry(stale: true) {
                return cached
            }
            return AssistantWidgetEntry(
                date: .now,
                tasks: [],
                reminders: [],
                events: [],
                calendarUnavailable: false,
                calendarPending: false,
                stale: false,
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

    private var manualFocusTask: WidgetTask? {
        entry.tasks.first(where: { $0.isFocused })
    }

    private var activeEvent: WidgetEvent? {
        entry.events.first { $0.start <= entry.date && $0.end > entry.date }
    }

    private var dueReminder: WidgetReminder? {
        entry.reminders.first(where: { reminder in
            guard let at = reminder.at else { return false }
            return at <= entry.date
        })
    }

    private var upcomingEvent: WidgetEvent? {
        guard manualFocusTask == nil, activeEvent == nil, dueReminder == nil else { return nil }
        let cutoff = entry.date.addingTimeInterval(15 * 60)
        return entry.events.first {
            $0.start > entry.date && $0.start <= cutoff && $0.end > entry.date
        }
    }

    private var focusEvent: WidgetEvent? {
        if let activeEvent { return activeEvent }
        if let upcomingEvent { return upcomingEvent }
        return nil
    }

    private var focusReminder: WidgetReminder? {
        guard activeEvent == nil else { return nil }
        return dueReminder
    }

    private var focusTaskItem: WidgetTask? {
        guard focusEvent == nil, focusReminder == nil else { return nil }
        return manualFocusTask
    }

    private var nextEvent: WidgetEvent? {
        entry.events.first {
            $0.end > entry.date && $0.id != focusEvent?.id
        }
    }

    private var nextReminder: WidgetReminder? {
        entry.reminders.first {
            $0.id != focusReminder?.id
        }
    }

    private var nextTaskLimit: Int {
        let focusRows = (focusEvent != nil || focusReminder != nil || focusTaskItem != nil) ? 1 : 0
        let timedRows = (nextEvent == nil ? 0 : 1) + (nextReminder == nil ? 0 : 1)
        return max(0, visibleTaskCount - focusRows - timedRows)
    }

    private var nextTasks: [WidgetTask] {
        entry.tasks
            .filter { $0.id != focusTaskItem?.id }
            .prefix(nextTaskLimit)
            .map { $0 }
    }

    private var hasNextContent: Bool {
        nextEvent != nil || nextReminder != nil || !nextTasks.isEmpty
    }

    @ViewBuilder
    private var nextContent: some View {
        if hasNextContent {
            Text("Дальше")
                .font(.caption2.weight(.semibold))
                .foregroundStyle(.secondary)

            nextTimedContent

            ForEach(nextTasks) { task in
                compactTask(task)
            }
        }
    }

    @ViewBuilder
    private var nextTimedContent: some View {
        if let event = nextEvent, let reminder = nextReminder, let reminderAt = reminder.at {
            if reminderAt <= event.start {
                compactReminder(reminder)
                compactEvent(event)
            } else {
                compactEvent(event)
                compactReminder(reminder)
            }
        } else {
            if let event = nextEvent {
                compactEvent(event)
            }
            if let reminder = nextReminder {
                compactReminder(reminder)
            }
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: family == .systemLarge ? 10 : 7) {
            header

            if let error = entry.error {
                errorState(error)
            } else if let event = focusEvent {
                focusEventView(event)
                nextContent
                Spacer(minLength: 0)
            } else if let reminder = focusReminder {
                focusReminderView(reminder)
                nextContent
                Spacer(minLength: 0)
            } else if let focus = focusTaskItem {
                focusTask(focus)
                nextContent
                Spacer(minLength: 0)
            } else if hasNextContent {
                noFocusState
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

            if entry.calendarPending {
                ProgressView()
                    .controlSize(.small)
                    .accessibilityLabel("Календарь загружается")
            } else if entry.calendarUnavailable {
                Image(systemName: "calendar.badge.exclamationmark")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .accessibilityLabel("Календарь не обновился")
            }

            if entry.stale {
                Image(systemName: "wifi.slash")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .accessibilityLabel("Показаны сохранённые данные")
            }

            Spacer()

            if entry.error != nil || entry.stale {
                Button(intent: RefreshAssistantWidgetIntent()) {
                    Image(systemName: "arrow.clockwise")
                        .font(.subheadline.weight(.semibold))
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Обновить")
            }

            Link(destination: URL(string: "assistantpocket://capture?mode=voice")!) {
                Image(systemName: "mic.circle.fill")
                    .font(.title3)
            }
            .accessibilityLabel("Записать голосом")

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
                Image(systemName: "circle")
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
                Text(taskDeadlineText(deadline))
                    .font(.caption2)
                    .foregroundStyle(task.overdue ? .red : .secondary)
            }

            if task.isFocused {
                Text("вернуться")
                    .font(.caption2.weight(.medium))
                    .foregroundStyle(.secondary)
            } else {
                Button(intent: FocusTaskIntent(taskID: task.id)) {
                    Text("Сейчас")
                        .font(.caption2.weight(.medium))
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Сейчас")
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
                Text(taskDeadlineText(deadline))
                    .foregroundStyle(task.overdue ? .red : .secondary)
            }

            if task.inProgress {
                Text("в работе")
            }

            if task.isFocused, let focusedSince = task.focusedSince {
                Text("с")
                Text(focusedSince, format: .dateTime.hour().minute())
            }
        }
        .font(.caption2)
        .foregroundStyle(.secondary)
    }

    private func focusEventView(_ event: WidgetEvent) -> some View {
        HStack(alignment: .top, spacing: 8) {
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

                if let paused = manualFocusTask {
                    Text("После: \(paused.title)")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
            }

            Spacer(minLength: 0)

            Button(intent: DismissCalendarEventIntent(eventID: event.id, eventEnd: event.end)) {
                Image(systemName: "xmark.circle.fill")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Убрать встречу из внимания")
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
        HStack(alignment: .top, spacing: 8) {
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

            Spacer(minLength: 0)

            Button(intent: SnoozeReminderIntent(reminderID: reminder.id)) {
                Text("+15")
                    .font(.caption2.weight(.semibold))
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Отложить на 15 минут")
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

            Button(intent: SnoozeReminderIntent(reminderID: reminder.id)) {
                Text("+15")
                    .font(.caption2.weight(.semibold))
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Отложить на 15 минут")
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

    private var noFocusState: some View {
        VStack(alignment: .leading, spacing: family == .systemLarge ? 8 : 5) {
            Text("Ничего не выбрано")
                .font(.subheadline.weight(.semibold))

            nextContent

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
