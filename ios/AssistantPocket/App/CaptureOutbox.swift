import Foundation

struct QueuedCapture: Codable, Identifiable {
    let id: UUID
    let text: String
    let context: String?
    let createdAt: Date
}

enum CaptureOutbox {
    private static let key = "assistant.captureOutbox"

    static func all() -> [QueuedCapture] {
        guard let data = UserDefaults.standard.data(forKey: key) else { return [] }
        return (try? JSONDecoder().decode([QueuedCapture].self, from: data)) ?? []
    }

    @discardableResult
    static func enqueue(text: String, context: String?) -> QueuedCapture {
        let item = QueuedCapture(
            id: UUID(),
            text: text,
            context: context,
            createdAt: .now
        )
        var items = all()
        items.append(item)
        save(items)
        return item
    }

    static func remove(_ id: UUID) {
        save(all().filter { $0.id != id })
    }

    private static func save(_ items: [QueuedCapture]) {
        if items.isEmpty {
            UserDefaults.standard.removeObject(forKey: key)
            return
        }
        guard let data = try? JSONEncoder().encode(items) else { return }
        UserDefaults.standard.set(data, forKey: key)
    }
}
