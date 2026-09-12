import Foundation

@MainActor
final class AppSettings: ObservableObject {
    private static let baseURLKey = "assistant.baseURL"
    private static let tokenAccount = "companion-api-token"

    @Published var baseURL: String {
        didSet {
            UserDefaults.standard.set(baseURL.trimmingCharacters(in: .whitespacesAndNewlines), forKey: Self.baseURLKey)
        }
    }

    @Published var token: String {
        didSet {
            KeychainStore.write(token.trimmingCharacters(in: .whitespacesAndNewlines), account: Self.tokenAccount)
        }
    }

    init() {
        self.baseURL = UserDefaults.standard.string(forKey: Self.baseURLKey) ?? ""
        self.token = KeychainStore.read(Self.tokenAccount)
    }

    var isConfigured: Bool {
        URL(string: normalizedBaseURL) != nil && !token.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    var normalizedBaseURL: String {
        baseURL.trimmingCharacters(in: CharacterSet(charactersIn: " /\n\t"))
    }
}
