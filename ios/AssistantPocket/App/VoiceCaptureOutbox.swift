import Foundation

struct QueuedVoiceCapture: Codable, Identifiable {
    let id: UUID
    let filename: String
    let context: String?
    let createdAt: Date
}

enum VoiceCaptureOutbox {
    private static let key = "assistant.voiceCaptureOutbox"

    private static var directory: URL {
        let base = FileManager.default.urls(
            for: .applicationSupportDirectory,
            in: .userDomainMask
        ).first ?? FileManager.default.temporaryDirectory
        let directory = base.appendingPathComponent("AssistantVoiceOutbox", isDirectory: true)
        try? FileManager.default.createDirectory(
            at: directory,
            withIntermediateDirectories: true
        )
        return directory
    }

    static func all() -> [QueuedVoiceCapture] {
        let stored = storedItems()
        var recovered = stored.filter {
            FileManager.default.fileExists(atPath: fileURL(for: $0).path)
        }
        var knownFilenames = Set(recovered.map(\.filename))

        // If the app was terminated after the recording moved into
        // Application Support but before UserDefaults metadata was written,
        // recover the UUID-named audio file instead of silently orphaning it.
        if let files = try? FileManager.default.contentsOfDirectory(
            at: directory,
            includingPropertiesForKeys: [.creationDateKey],
            options: [.skipsHiddenFiles]
        ) {
            for url in files where url.pathExtension.lowercased() == "m4a" {
                let filename = url.lastPathComponent
                guard !knownFilenames.contains(filename),
                      let id = UUID(uuidString: url.deletingPathExtension().lastPathComponent) else {
                    continue
                }

                let values = try? url.resourceValues(forKeys: [.creationDateKey])
                recovered.append(
                    QueuedVoiceCapture(
                        id: id,
                        filename: filename,
                        context: nil,
                        createdAt: values?.creationDate ?? .now
                    )
                )
                knownFilenames.insert(filename)
            }
        }

        recovered.sort { $0.createdAt < $1.createdAt }
        if recovered.map(\.filename) != stored.map(\.filename) {
            save(recovered)
        }
        return recovered
    }

    private static func storedItems() -> [QueuedVoiceCapture] {
        guard let data = UserDefaults.standard.data(forKey: key),
              let items = try? JSONDecoder().decode([QueuedVoiceCapture].self, from: data) else {
            return []
        }
        return items
    }

    @discardableResult
    static func enqueue(recordingURL: URL, context: String?) throws -> QueuedVoiceCapture {
        let id = UUID()
        let filename = "\(id.uuidString.lowercased()).m4a"
        let destination = directory.appendingPathComponent(filename)

        var items = all()

        if FileManager.default.fileExists(atPath: destination.path) {
            try FileManager.default.removeItem(at: destination)
        }
        try FileManager.default.moveItem(at: recordingURL, to: destination)

        let item = QueuedVoiceCapture(
            id: id,
            filename: filename,
            context: context,
            createdAt: .now
        )
        items.append(item)
        save(items)
        return item
    }

    static func data(for item: QueuedVoiceCapture) throws -> Data {
        try Data(contentsOf: fileURL(for: item))
    }

    static func url(for item: QueuedVoiceCapture) -> URL {
        fileURL(for: item)
    }

    static func remove(_ id: UUID) {
        let items = all()
        if let item = items.first(where: { $0.id == id }) {
            try? FileManager.default.removeItem(at: fileURL(for: item))
        }
        save(items.filter { $0.id != id })
    }

    private static func fileURL(for item: QueuedVoiceCapture) -> URL {
        directory.appendingPathComponent(item.filename)
    }

    private static func save(_ items: [QueuedVoiceCapture]) {
        if items.isEmpty {
            UserDefaults.standard.removeObject(forKey: key)
            return
        }
        guard let data = try? JSONEncoder().encode(items) else { return }
        UserDefaults.standard.set(data, forKey: key)
    }
}
