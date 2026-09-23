import AppIntents
import Foundation

enum CaptureLaunchMode: String {
    case text
    case voice
}

enum CaptureLaunchSignal {
    static let notification = Notification.Name("assistant.capture.requested")

    @MainActor
    static func request(mode: CaptureLaunchMode = .text) {
        WidgetSharedSettings.requestCaptureLaunch(mode: mode.rawValue)
        NotificationCenter.default.post(
            name: notification,
            object: mode.rawValue
        )
    }

    static func mode(from notification: Notification) -> CaptureLaunchMode {
        guard let raw = notification.object as? String,
              let mode = CaptureLaunchMode(rawValue: raw) else {
            return .text
        }
        return mode
    }

    static func consumeMode() -> CaptureLaunchMode? {
        guard let raw = WidgetSharedSettings.consumeCaptureLaunchMode() else {
            return nil
        }
        return CaptureLaunchMode(rawValue: raw) ?? .text
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
        CaptureLaunchSignal.request(mode: .text)
        return .result()
    }
}
