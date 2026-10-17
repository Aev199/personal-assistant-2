import AppIntents

@available(iOS 18.0, *)
struct AssistantAppShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(
            intent: OpenAssistantCaptureIntent(),
            phrases: [
                "Быстрый ввод в \(.applicationName)",
                "Записать в \(.applicationName)"
            ],
            shortTitle: "Быстрый ввод",
            systemImageName: "plus.circle.fill"
        )
        AppShortcut(
            intent: OpenAssistantVoiceCaptureIntent(),
            phrases: [
                "Голосовая запись в \(.applicationName)",
                "Надиктовать в \(.applicationName)"
            ],
            shortTitle: "Голосовая запись",
            systemImageName: "mic.circle.fill"
        )
    }
}
