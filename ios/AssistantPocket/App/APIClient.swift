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
        request.timeoutInterval = 20
        request.setValue("Bearer \(cleanToken)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = body
        }
        return request
    }

    private func send(
        path: String,
        legacyPath: String? = nil,
        method: String = "GET",
        body: Data? = nil
    ) async throws -> (Data, URLResponse) {
        let primary = try request(path: path, method: method, body: body)
        let (data, response) = try await URLSession.shared.data(for: primary)

        if let http = response as? HTTPURLResponse,
           http.statusCode == 404,
           let legacyPath {
            let legacy = try request(path: legacyPath, method: method, body: body)
            return try await URLSession.shared.data(for: legacy)
        }

        return (data, response)
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
        let (data, response) = try await send(
            path: "/api/v1/today",
            legacyPath: "/api/v1/companion/today"
        )
        return try decode(TodayResponse.self, data: data, response: response)
    }

    func loadTasks(limit: Int = 100) async throws -> TasksResponse {
        let safeLimit = max(1, min(200, limit))
        let (data, response) = try await send(path: "/api/v1/tasks?limit=\(safeLimit)")
        return try decode(TasksResponse.self, data: data, response: response)
    }

    func loadProjects() async throws -> ProjectsResponse {
        let req = try request(path: "/api/v1/projects")
        let (data, response) = try await URLSession.shared.data(for: req)
        return try decode(ProjectsResponse.self, data: data, response: response)
    }

    func updateTask(
        taskID: Int,
        title: String,
        projectCode: String?,
        deadline: Date?
    ) async throws -> TaskMutationResponse {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]

        var payload: [String: Any] = [
            "title": title,
            "deadline": NSNull(),
        ]
        if let projectCode, !projectCode.isEmpty {
            payload["project_code"] = projectCode
        }
        if let deadline {
            payload["deadline"] = formatter.string(from: deadline)
        }

        let body = try JSONSerialization.data(withJSONObject: payload)
        let req = try request(
            path: "/api/v1/tasks/\(taskID)",
            method: "PATCH",
            body: body
        )
        let (data, response) = try await URLSession.shared.data(for: req)
        return try decode(TaskMutationResponse.self, data: data, response: response)
    }

    func intake(
        _ text: String,
        context: String? = nil,
        clientID: UUID? = nil
    ) async throws -> NativeIntakeResponse {
        var payload: [String: Any] = ["text": text]
        if let context, !context.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            payload["context"] = context
        }
        if let clientID {
            payload["client_id"] = clientID.uuidString.lowercased()
        }
        let body = try JSONSerialization.data(withJSONObject: payload)
        let req = try request(path: "/api/v1/intake", method: "POST", body: body)
        let (data, response) = try await URLSession.shared.data(for: req)
        return try decode(NativeIntakeResponse.self, data: data, response: response)
    }

    func loadPendingIntake() async throws -> NativePendingListResponse {
        let req = try request(path: "/api/v1/intake/pending")
        let (data, response) = try await URLSession.shared.data(for: req)
        return try decode(NativePendingListResponse.self, data: data, response: response)
    }

    func confirmIntake(pendingActionID: Int) async throws -> NativePendingMutationResponse {
        let req = try request(
            path: "/api/v1/intake/\(pendingActionID)/confirm",
            method: "POST",
            body: Data("{}".utf8)
        )
        let (data, response) = try await URLSession.shared.data(for: req)
        return try decode(NativePendingMutationResponse.self, data: data, response: response)
    }

    func cancelIntake(pendingActionID: Int) async throws -> NativePendingMutationResponse {
        let req = try request(
            path: "/api/v1/intake/\(pendingActionID)/cancel",
            method: "POST",
            body: Data("{}".utf8)
        )
        let (data, response) = try await URLSession.shared.data(for: req)
        return try decode(NativePendingMutationResponse.self, data: data, response: response)
    }

    func capture(_ text: String) async throws -> CaptureResponse {
        let data = try JSONSerialization.data(withJSONObject: ["text": text])
        let (responseData, response) = try await send(
            path: "/api/v1/capture",
            legacyPath: "/api/v1/companion/capture",
            method: "POST",
            body: data
        )
        return try decode(CaptureResponse.self, data: responseData, response: response)
    }

    func focusTask(taskID: Int) async throws -> DoneResponse {
        let req = try request(
            path: "/api/v1/tasks/\(taskID)/focus",
            method: "POST",
            body: Data("{}".utf8)
        )
        let (data, response) = try await URLSession.shared.data(for: req)
        return try decode(DoneResponse.self, data: data, response: response)
    }

    func markDone(taskID: Int) async throws -> DoneResponse {
        let body = Data("{}".utf8)
        let (data, response) = try await send(
            path: "/api/v1/tasks/\(taskID)/done",
            legacyPath: "/api/v1/companion/tasks/\(taskID)/done",
            method: "POST",
            body: body
        )
        return try decode(DoneResponse.self, data: data, response: response)
    }
}
