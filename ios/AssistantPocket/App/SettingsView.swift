import SwiftUI

struct SettingsView: View {
    @EnvironmentObject private var settings: AppSettings
    @Environment(\.dismiss) private var dismiss

    @State private var baseURLDraft = ""
    @State private var tokenDraft = ""
    @State private var didLoadDraft = false

    private var normalizedDraftURL: String {
        baseURLDraft.trimmingCharacters(in: CharacterSet(charactersIn: " /\n\t"))
    }

    private var isDraftConfigured: Bool {
        AppSettings.isValidBaseURL(normalizedDraftURL)
            && !tokenDraft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    var body: some View {
        NavigationStack {
            Form {
                Section("Подключение") {
                    TextField("Адрес Assistant", text: $baseURLDraft)
                        .textInputAutocapitalization(.never)
                        .keyboardType(.URL)
                        .autocorrectionDisabled()

                    SecureField("Код доступа", text: $tokenDraft)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                }

                Section {
                    Text("Эти данные нужны только один раз для подключения к вашему Assistant.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
            }
            .navigationTitle("Assistant")
            .toolbar {
                if settings.isConfigured {
                    ToolbarItem(placement: .cancellationAction) {
                        Button("Отмена") {
                            dismiss()
                        }
                    }
                }

                ToolbarItem(placement: .confirmationAction) {
                    Button("Сохранить") {
                        settings.baseURL = normalizedDraftURL
                        settings.token = tokenDraft.trimmingCharacters(in: .whitespacesAndNewlines)
                        settings.syncWidgetSettings()
                        dismiss()
                    }
                    .disabled(!isDraftConfigured)
                }
            }
            .onAppear {
                guard !didLoadDraft else { return }
                baseURLDraft = settings.baseURL
                tokenDraft = settings.token
                didLoadDraft = true
            }
            .interactiveDismissDisabled(!settings.isConfigured)
        }
    }
}
