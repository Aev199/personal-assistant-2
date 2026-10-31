import Foundation

struct TodayResponse: Decodable {
    let ok: Bool
    let date: String
    let timezone: String
    let tasks: [TodayTask]
    let reminders: [TodayReminder]
    let events: [TodayEvent]?
    let calendarUnavailable: Bool?
    let calendarPending: Bool?

    enum CodingKeys: String, CodingKey {
        case ok, date, timezone, tasks, reminders, events
        case calendarUnavailable = "calendar_unavailable"
        case calendarPending = "calendar_pending"
    }
}

struct TodayEvent: Decodable, Identifiable {
    let id: String
    let title: String
    let start: Date
    let end: Date
    let kind: String
}

struct TasksResponse: Decodable {
    let ok: Bool
    let timezone: String
    let tasks: [TodayTask]
}

struct IdeasResponse: Decodable {
    let ok: Bool
    let ideas: [AssistantIdea]
}

struct AssistantIdea: Decodable, Identifiable {
    let id: Int
    let text: String
}

struct IdeaMutationResponse: Decodable {
    let ok: Bool
    let ideaId: Int
    let taskId: Int?
    let status: String

    enum CodingKeys: String, CodingKey {
        case ok, status
        case ideaId = "idea_id"
        case taskId = "task_id"
    }
}

struct ProjectsResponse: Decodable {
    let ok: Bool
    let projects: [AssistantProject]
}

struct AssistantProject: Decodable, Identifiable, Hashable {
    let id: Int
    let code: String
    let name: String

    var displayName: String {
        if code.uppercased() == "INBOX" { return "Входящие" }
        return name.isEmpty || name == code ? code : "\(code) · \(name)"
    }
}

struct TaskMutationResponse: Decodable {
    let ok: Bool
    let taskId: Int
    let status: String

    enum CodingKeys: String, CodingKey {
        case ok, status
        case taskId = "task_id"
    }
}

struct TodayTask: Decodable, Identifiable {
    let id: Int
    let title: String
    let project: String
    let kind: String?
    let assignee: String
    let status: String?
    let deadline: Date?
    let overdue: Bool
    /// Server edit version, sent back unchanged so an edit cannot silently
    /// overwrite a change made from the web or Telegram.
    let updatedAt: String?
    /// On the shared «На сегодня» list: planned for today, or due today.
    let todayPlan: Bool?

    enum CodingKeys: String, CodingKey {
        case id, title, project, kind, assignee, status, deadline, overdue
        case updatedAt = "updated_at"
        case todayPlan = "today_plan"
    }

    var isInTodayPlan: Bool {
        todayPlan == true
    }

    var inProgress: Bool {
        status?.lowercased() == "in_progress"
    }

    var isPersonal: Bool {
        kind?.lowercased() == "personal"
    }
}

struct TodayReminder: Decodable, Identifiable {
    let id: Int
    let text: String
    let at: Date?
}

struct NativePendingListResponse: Decodable {
    let ok: Bool
    let pending: [NativeIntakePending]
}

struct NativeIntakeResponse: Decodable {
    let ok: Bool
    let captureId: String?
    let status: String
    let message: String?
    let saved: [NativeIntakeItem]
    let needsInput: [NativeIntakeNeedInput]
    let pending: [NativeIntakePending]
    let context: String?
    let transcript: String?
    /// The model was unavailable, so the text went to Inbox as is.
    let inboxFallback: Bool?

    enum CodingKeys: String, CodingKey {
        case ok, status, message, saved, pending, context, transcript
        case captureId = "capture_id"
        case needsInput = "needs_input"
        case inboxFallback = "inbox_fallback"
    }
}

struct NativeIntakeItem: Decodable, Identifiable {
    let status: String
    let kind: String
    let title: String
    let pendingActionId: Int?

    var id: Int { pendingActionId ?? title.hashValue }

    enum CodingKeys: String, CodingKey {
        case status, kind, title
        case pendingActionId = "pending_action_id"
    }
}

struct NativeIntakeNeedInput: Decodable, Identifiable {
    let status: String
    let action: String
    let title: String
    let prompt: String

    var id: String { "\(action):\(title):\(prompt)" }
}

struct NativeIntakePending: Decodable, Identifiable {
    let status: String
    let kind: String
    let title: String
    let pendingActionId: Int
    let payload: NativeIntakePendingPayload?

    var id: Int { pendingActionId }

    enum CodingKeys: String, CodingKey {
        case status, kind, title, payload
        case pendingActionId = "pending_action_id"
    }
}

struct NativeIntakePendingPayload: Decodable {
    let startLocal: Date?
    let durationMin: Int?
    let calendarKind: String?

    enum CodingKeys: String, CodingKey {
        case startLocal = "start_local"
        case durationMin = "duration_min"
        case calendarKind = "calendar_kind"
    }
}

struct NativePendingMutationResponse: Decodable {
    let ok: Bool
    let status: String?
    let pendingActionId: Int?
    let message: String?
    let error: String?

    enum CodingKeys: String, CodingKey {
        case ok, status, message, error
        case pendingActionId = "pending_action_id"
    }
}

struct AttentionMutationResponse: Decodable {
    let ok: Bool
    let status: String
}

struct DoneResponse: Decodable {
    let ok: Bool
    let taskId: Int
    let status: String

    enum CodingKeys: String, CodingKey {
        case ok
        case taskId = "task_id"
        case status
    }
}

struct APIErrorResponse: Decodable {
    let ok: Bool?
    let error: String?
}

struct RemindersResponse: Decodable {
    let ok: Bool
    let reminders: [UpcomingReminder]
}

struct UpcomingReminder: Decodable, Identifiable {
    let id: Int
    let text: String
    let at: Date?
    let repeatRule: String?

    enum CodingKeys: String, CodingKey {
        case id, text, at
        case repeatRule = "repeat"
    }

    var repeatLabel: String? {
        switch repeatRule {
        case "daily": return "каждый день"
        case "weekly": return "каждую неделю"
        case "monthly": return "каждый месяц"
        case "workdays": return "по будням"
        default: return nil
        }
    }
}

struct ReminderMutationResponse: Decodable {
    let ok: Bool
    let status: String
}
