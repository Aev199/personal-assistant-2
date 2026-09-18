import AppIntents
import Foundation

enum CaptureLaunchSignal {
    static let defaultsKey = "assistant.capture.requested"
    static let notification = Notification.Name("assistant.capture.requested")

    @MainActor
    static func request() {
        UserDefaults.standard.set(true, forKey: defaultsKey)
        NotificationCenter.default.post(name: notification, object: nil)
    }

    static func consume() -> Bool {
        guard UserDefaults.standard.bool(forKey: defaultsKey) else { return false }
        UserDefaults.standard.removeObject(forKey: defaultsKey)
        return true
    }
}

@available(iOS 18.0, *)
enum AssistantOpenTarget: String, AppEnum {
    case capture

    static var typeDisplayRepresentation = TypeDisplayRepresentation("Assistant")
    static var caseDisplayRepresentations: [AssistantOpenTarget: DisplayRepresentation] = [
        .capture: DisplayRepresentation(title: "Быстрый ввод")
    ]
}

@available(iOS 18.0, *)
struct OpenAssistantCaptureIntent: OpenIntent {
    static var title: LocalizedStringResource = "Быстрый ввод"
    static var description = IntentDescription("Открывает Personal Assistant сразу в поле быстрого ввода.")

    @Parameter(title: "Экран")
    var target: AssistantOpenTarget

    init() {
        target = .capture
    }

    init(target: AssistantOpenTarget) {
        self.target = target
    }

    @MainActor
    func perform() async throws -> some IntentResult {
        CaptureLaunchSignal.request()
        return .result()
    }
}
