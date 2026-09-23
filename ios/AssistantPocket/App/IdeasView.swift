import SwiftUI
import WidgetKit

struct IdeasView: View {
    @EnvironmentObject private var settings: AppSettings

    let refreshToken: Int
    let onTaskCreated: () -> Void

    @State private var ideas: [AssistantIdea] = []
    @State private var loadGeneration = 0
    @State private var isLoading = false
    @State private var dataStale = false
    @State private var errorMessage: String?
    @State private var actionError: String?

    var body: some View {
        Group {
            if ideas.isEmpty && isLoading {
                ProgressView("Загружаю идеи…")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if ideas.isEmpty, let errorMessage {
                ContentUnavailableView(
                    "Не удалось загрузить идеи",
                    systemImage: "wifi.exclamationmark",
                    description: Text(errorMessage)
                )
            } else if ideas.isEmpty {
                ContentUnavailableView(
                    "Идей пока нет",
                    systemImage: "lightbulb",
                    description: Text("Сохранённые мысли появляются здесь и не попадают в список дел.")
                )
            } else {
                VStack(spacing: 0) {
                    staleBanner

                    List {
                        ForEach(ideas) { idea in
                        Text(idea.text)
                            .font(.body)
                            .fixedSize(horizontal: false, vertical: true)
                            .padding(.vertical, 4)
                            .swipeActions(edge: .leading, allowsFullSwipe: false) {
                                Button {
                                    Task { await promote(idea) }
                                } label: {
                                    Label("В задачу", systemImage: "checklist")
                                }
                            }
                            .swipeActions(edge: .trailing, allowsFullSwipe: false) {
                                Button {
                                    Task { await archive(idea) }
                                } label: {
                                    Label("Архив", systemImage: "archivebox")
                                }
                            }
                            .contextMenu {
                                Button {
                                    Task { await promote(idea) }
                                } label: {
                                    Label("В задачу", systemImage: "checklist")
                                }

                                Button {
                                    Task { await archive(idea) }
                                } label: {
                                    Label("Архив", systemImage: "archivebox")
                                }
                            }
                        }
                    }
                    .listStyle(.insetGrouped)
                    .refreshable {
                        await load()
                    }
                }
            }
        }
        .navigationTitle("Идеи")
        .navigationBarTitleDisplayMode(.large)
        .toolbar {
            ToolbarItemGroup(placement: .topBarTrailing) {
                Link(destination: URL(string: "assistantpocket://capture?mode=voice")!) {
                    Image(systemName: "mic")
                }
                .accessibilityLabel("Записать голосом")

                Link(destination: URL(string: "assistantpocket://capture")!) {
                    Image(systemName: "plus")
                }
                .accessibilityLabel("Запомнить")
            }
        }
        .task {
            await load()
        }
        .onChange(of: refreshToken) { _, _ in
            Task { await load() }
        }
        .alert("Не удалось выполнить действие", isPresented: actionErrorPresented) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(actionError ?? "")
        }
    }

    @ViewBuilder
    private var staleBanner: some View {
        if dataStale {
            HStack(spacing: 8) {
                Label("Показаны последние данные", systemImage: "wifi.slash")
                    .font(.caption)
                    .foregroundStyle(.secondary)

                Spacer(minLength: 8)

                Button {
                    Task { await load() }
                } label: {
                    Image(systemName: "arrow.clockwise")
                }
                .buttonStyle(.plain)
                .foregroundStyle(.secondary)
                .accessibilityLabel("Повторить обновление идей")
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 6)
        }
    }

    private var actionErrorPresented: Binding<Bool> {
        Binding(
            get: { actionError != nil },
            set: { isPresented in
                if !isPresented {
                    actionError = nil
                }
            }
        )
    }

    @MainActor
    private func load() async {
        guard settings.isConfigured else { return }

        loadGeneration += 1
        let generation = loadGeneration
        isLoading = true
        errorMessage = nil
        defer {
            if generation == loadGeneration {
                isLoading = false
            }
        }

        do {
            let client = APIClient(baseURL: settings.normalizedBaseURL, token: settings.token)
            let response = try await client.loadIdeas()
            guard generation == loadGeneration else { return }
            ideas = response.ideas
            dataStale = false
        } catch {
            guard generation == loadGeneration else { return }
            errorMessage = error.localizedDescription
            dataStale = !ideas.isEmpty
        }
    }

    @MainActor
    private func promote(_ idea: AssistantIdea) async {
        errorMessage = nil
        do {
            let client = APIClient(baseURL: settings.normalizedBaseURL, token: settings.token)
            _ = try await client.promoteIdea(ideaID: idea.id)
            ideas.removeAll { $0.id == idea.id }
            WidgetCenter.shared.reloadTimelines(ofKind: "AssistantPocketWidget")
            onTaskCreated()
        } catch {
            actionError = error.localizedDescription
        }
    }

    @MainActor
    private func archive(_ idea: AssistantIdea) async {
        errorMessage = nil
        do {
            let client = APIClient(baseURL: settings.normalizedBaseURL, token: settings.token)
            _ = try await client.archiveIdea(ideaID: idea.id)
            ideas.removeAll { $0.id == idea.id }
        } catch {
            actionError = error.localizedDescription
        }
    }
}
