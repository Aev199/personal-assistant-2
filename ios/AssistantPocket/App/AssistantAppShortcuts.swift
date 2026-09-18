import AppIntents

@available(iOS 18.0, *)
struct AssistantAppShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        [
            AppShortcut(
                intent: OpenAssistantCaptureIntent(),
                phrases: [
                    "Быстрый ввод в \(.applicationName)",
                    "Записать в \(.applicationName)"
                ],
                shortTitle: "Быстрый ввод",
                systemImageName: "plus.circle.fill"
            )
        ]
    }
}
