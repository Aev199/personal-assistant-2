import Foundation

struct TodayResponse: Decodable {
    let ok: Bool
    let date: String
    let timezone: String
    let tasks: [TodayTask]
    let reminders: [TodayReminder]
}

struct TasksResponse: Decodable {
    let ok: Bool
    let timezone: String
    let tasks: [TodayTask]
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
    let assignee: String
    let status: String?
    let deadline: Date?
    let overdue: Bool

    var inProgress: Bool {
        status?.lowercased() == "in_progress"
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

    enum CodingKeys: String, CodingKey {
        case ok, status, message, saved, pending, context
        case captureId = "capture_id"
        case needsInput = "needs_input"
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

    var id: Int { pendingActionId }

    enum CodingKeys: String, CodingKey {
        case status, kind, title
        case pendingActionId = "pending_action_id"
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

struct CaptureResponse: Decodable {
    let ok: Bool
    let task: CapturedTask
}

struct CapturedTask: Decodable {
    let id: Int
    let title: String
    let project: String
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
