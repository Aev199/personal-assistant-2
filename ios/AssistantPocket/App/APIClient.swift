import Foundation

enum APIClientError: LocalizedError {
    case notConfigured
    case invalidURL
    case http(Int, String)
    case invalidResponse

    var errorDescription: String? {
        switch self {
        case .notConfigured:
            return "Укажите адрес сервера и токен."
        case .invalidURL:
            return "Некорректный адрес сервера."
        case let .http(code, message):
            if code == 401 { return "Неверный код доступа." }
            if code == 503 { return "Assistant временно недоступен." }
            return message.isEmpty ? "Ошибка сервера: \(code)" : message
        case .invalidResponse:
            return "Сервер вернул неожиданный ответ."
        }
    }
}

struct APIClient {
    let baseURL: String
    let token: String

    private var decoder: JSONDecoder {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        return decoder
    }

    private func request(path: String, method: String = "GET", body: Data? = nil) throws -> URLRequest {
        let cleanBase = baseURL.trimmingCharacters(in: CharacterSet(charactersIn: " /\n\t"))
        let cleanToken = token.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleanBase.isEmpty, !cleanToken.isEmpty else { throw APIClientError.notConfigured }
        guard let url = URL(string: cleanBase + path) else { throw APIClientError.invalidURL }

        var request = URLRequest(url: url)
        request.httpMethod = method
        request.timeoutInterval = 15
        request.setValue("Bearer \(cleanToken)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = body
        }
        return request
    }

    private func decode<T: Decodable>(_ type: T.Type, data: Data, response: URLResponse) throws -> T {
        guard let http = response as? HTTPURLResponse else { throw APIClientError.invalidResponse }
        guard 200..<300 ~= http.statusCode else {
            let serverError = try? decoder.decode(APIErrorResponse.self, from: data)
            throw APIClientError.http(http.statusCode, serverError?.error ?? "")
        }
        return try decoder.decode(type, from: data)
    }

    func loadToday() async throws -> TodayResponse {
        let req = try request(path: "/api/v1/companion/today")
        let (data, response) = try await URLSession.shared.data(for: req)
        return try decode(TodayResponse.self, data: data, response: response)
    }

    func capture(_ text: String) async throws -> CaptureResponse {
        let data = try JSONSerialization.data(withJSONObject: ["text": text])
        let req = try request(path: "/api/v1/companion/capture", method: "POST", body: data)
        let (responseData, response) = try await URLSession.shared.data(for: req)
        return try decode(CaptureResponse.self, data: responseData, response: response)
    }

    func markDone(taskID: Int) async throws -> DoneResponse {
        let req = try request(path: "/api/v1/companion/tasks/\(taskID)/done", method: "POST", body: Data("{}".utf8))
        let (data, response) = try await URLSession.shared.data(for: req)
        return try decode(DoneResponse.self, data: data, response: response)
    }
}
