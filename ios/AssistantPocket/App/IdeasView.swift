import SwiftUI

struct IdeasView: View {
    @EnvironmentObject private var settings: AppSettings

    let onTaskCreated: () -> Void

    @State private var ideas: [AssistantIdea] = []
    @State private var isLoading = false
    @State private var errorMessage: String?

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
                List {
                    Section {
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
                        }
                    } footer: {
                        Text("Смахните идею: слева — в задачу, справа — в архив.")
                    }
                }
                .listStyle(.insetGrouped)
                .refreshable {
                    await load()
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
    }

    @MainActor
    private func load() async {
        guard settings.isConfigured else { return }

        isLoading = true
        errorMessage = nil
        defer { isLoading = false }

        do {
            let client = APIClient(baseURL: settings.normalizedBaseURL, token: settings.token)
            ideas = try await client.loadIdeas().ideas
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    @MainActor
    private func promote(_ idea: AssistantIdea) async {
        errorMessage = nil
        do {
            let client = APIClient(baseURL: settings.normalizedBaseURL, token: settings.token)
            _ = try await client.promoteIdea(ideaID: idea.id)
            ideas.removeAll { $0.id == idea.id }
            onTaskCreated()
        } catch {
            errorMessage = error.localizedDescription
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
            errorMessage = error.localizedDescription
        }
    }
}
