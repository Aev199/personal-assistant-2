import SwiftUI

private enum AppTab: Hashable {
    case today
    case tasks
    case ideas
}

struct AppRootView: View {
    @EnvironmentObject private var settings: AppSettings

    @State private var selectedTab: AppTab = .today
    @State private var taskRevision = 0

    var body: some View {
        TabView(selection: $selectedTab) {
            ContentView(refreshToken: taskRevision) {
                taskRevision += 1
            }
                .environmentObject(settings)
                .tabItem {
                    Label("Сегодня", systemImage: "house")
                }
                .tag(AppTab.today)

            NavigationStack {
                AllTasksView(refreshToken: taskRevision) {
                    taskRevision += 1
                }
                .environmentObject(settings)
            }
            .tabItem {
                Label("Задачи", systemImage: "checkmark.circle")
            }
            .tag(AppTab.tasks)

            NavigationStack {
                IdeasView(refreshToken: taskRevision) {
                    taskRevision += 1
                }
                .environmentObject(settings)
            }
            .tabItem {
                Label("Идеи", systemImage: "lightbulb")
            }
            .tag(AppTab.ideas)
        }
        .onOpenURL { url in
            guard url.scheme == "assistantpocket", url.host == "capture" else { return }
            selectedTab = .today

            let mode = URLComponents(url: url, resolvingAgainstBaseURL: false)?
                .queryItems?
                .first(where: { $0.name == "mode" })?
                .value

            DispatchQueue.main.async {
                CaptureLaunchSignal.request(mode: mode == "voice" ? .voice : .text)
            }
        }
    }
}
