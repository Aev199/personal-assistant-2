import SwiftUI

@main
struct AssistantPocketApp: App {
    @StateObject private var settings = AppSettings()

    var body: some Scene {
        WindowGroup {
            AppRootView()
                .environmentObject(settings)
        }
    }
}
