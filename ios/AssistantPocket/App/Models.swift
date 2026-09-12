import Foundation

struct TodayResponse: Decodable {
    let ok: Bool
    let date: String
    let timezone: String
    let tasks: [TodayTask]
    let reminders: [TodayReminder]
}

struct TodayTask: Decodable, Identifiable {
    let id: Int
    let title: String
    let project: String
    let assignee: String
    let deadline: Date?
    let overdue: Bool
}

struct TodayReminder: Decodable, Identifiable {
    let id: Int
    let text: String
    let at: Date?
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
