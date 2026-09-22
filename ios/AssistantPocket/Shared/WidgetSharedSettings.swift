import Foundation
import Security

enum WidgetSharedSettings {
    private static let service = "com.aev199.assistantpocket.widget-shared"
    private static let baseURLAccount = "base-url"
    private static let tokenAccount = "token"
    private static let captureRequestAccount = "capture-request"
    private static let todayCacheAccount = "today-cache"
    private static let preferTodayCacheAccount = "prefer-today-cache"

    static var baseURL: String {
        read(baseURLAccount)
            .trimmingCharacters(in: CharacterSet(charactersIn: " /\n\t"))
    }

    static var token: String {
        read(tokenAccount)
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }

    static var cachedTodayData: Data? {
        let encoded = read(todayCacheAccount)
        guard !encoded.isEmpty else { return nil }
        return Data(base64Encoded: encoded)
    }

    static func write(baseURL: String, token: String) {
        writeValue(
            baseURL.trimmingCharacters(in: CharacterSet(charactersIn: " /\n\t")),
            account: baseURLAccount
        )
        writeValue(
            token.trimmingCharacters(in: .whitespacesAndNewlines),
            account: tokenAccount
        )
    }

    static func writeCachedTodayData(_ data: Data) {
        writeValue(data.base64EncodedString(), account: todayCacheAccount)
    }

    static func clearCachedTodayData() {
        delete(todayCacheAccount)
    }

    static func requestCachedTodayOnce() {
        writeValue("1", account: preferTodayCacheAccount)
    }

    static func consumeCachedTodayOnce() -> Bool {
        guard read(preferTodayCacheAccount) == "1" else { return false }
        delete(preferTodayCacheAccount)
        return true
    }

    static func requestCaptureLaunch() {
        writeValue("1", account: captureRequestAccount)
    }

    static func consumeCaptureLaunch() -> Bool {
        guard read(captureRequestAccount) == "1" else { return false }
        delete(captureRequestAccount)
        return true
    }

    private static func read(_ account: String) -> String {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]

        var item: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess,
              let data = item as? Data,
              let value = String(data: data, encoding: .utf8) else {
            return ""
        }
        return value
    }

    private static func writeValue(_ value: String, account: String) {
        let data = Data(value.utf8)
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        let attributes: [String: Any] = [
            kSecValueData as String: data,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
        ]

        let status = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)
        if status == errSecItemNotFound {
            var add = query
            add[kSecValueData as String] = data
            add[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
            SecItemAdd(add as CFDictionary, nil)
        }
    }

    private static func delete(_ account: String) {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        SecItemDelete(query as CFDictionary)
    }
}
