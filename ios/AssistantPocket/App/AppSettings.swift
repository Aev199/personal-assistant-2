import Foundation
import WidgetKit

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
        syncWidgetSettings()
    }

    var isConfigured: Bool {
        Self.isValidBaseURL(normalizedBaseURL)
            && !token.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    static func isValidBaseURL(_ rawValue: String) -> Bool {
        let clean = rawValue.trimmingCharacters(in: CharacterSet(charactersIn: " /\n\t"))
        guard let url = URL(string: clean),
              let scheme = url.scheme?.lowercased(),
              scheme == "http" || scheme == "https",
              url.host != nil else {
            return false
        }
        return true
    }

    var normalizedBaseURL: String {
        baseURL.trimmingCharacters(in: CharacterSet(charactersIn: " /\n\t"))
    }

    func syncWidgetSettings() {
        WidgetSharedSettings.write(baseURL: normalizedBaseURL, token: token)
        WidgetCenter.shared.reloadAllTimelines()
    }
}
