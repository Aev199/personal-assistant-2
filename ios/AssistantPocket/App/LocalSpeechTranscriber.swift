import AVFoundation
import Foundation
import Speech

enum LocalSpeechTranscriber {
    static func transcribe(fileURL: URL) async -> String? {
        guard #available(iOS 26.0, *) else { return nil }

        let preferredLocales = [
            Locale(identifier: "ru-RU"),
            Locale.current,
        ]

        for requestedLocale in preferredLocales {
            if let text = try? await transcribeWithSpeechTranscriber(
                fileURL: fileURL,
                requestedLocale: requestedLocale
            ), let clean = cleaned(text) {
                return clean
            }
        }

        for requestedLocale in preferredLocales {
            if let text = try? await transcribeWithDictationTranscriber(
                fileURL: fileURL,
                requestedLocale: requestedLocale
            ), let clean = cleaned(text) {
                return clean
            }
        }

        return nil
    }

    private static func cleaned(_ text: String?) -> String? {
        guard let text else { return nil }
        let clean = text
            .replacingOccurrences(of: "\n", with: " ")
            .split(whereSeparator: { $0.isWhitespace })
            .joined(separator: " ")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return clean.isEmpty ? nil : clean
    }

    @available(iOS 26.0, *)
    private static func transcribeWithSpeechTranscriber(
        fileURL: URL,
        requestedLocale: Locale
    ) async throws -> String? {
        guard SpeechTranscriber.isAvailable,
              let locale = await SpeechTranscriber.supportedLocale(
                equivalentTo: requestedLocale
              ) else {
            return nil
        }

        let transcriber = SpeechTranscriber(
            locale: locale,
            preset: .offlineTranscription
        )
        try await ensureAssets(for: [transcriber])

        let audioFile = try AVAudioFile(forReading: fileURL)
        async let transcriptTask: String = {
            var output = ""
            for try await result in transcriber.results {
                if result.isFinal {
                    output += String(result.text.characters)
                }
            }
            return output
        }()

        let analyzer = SpeechAnalyzer(modules: [transcriber])
        if let lastSample = try await analyzer.analyzeSequence(from: audioFile) {
            try await analyzer.finalizeAndFinish(through: lastSample)
        } else {
            await analyzer.cancelAndFinishNow()
        }

        return try await transcriptTask
    }

    @available(iOS 26.0, *)
    private static func transcribeWithDictationTranscriber(
        fileURL: URL,
        requestedLocale: Locale
    ) async throws -> String? {
        guard let locale = await DictationTranscriber.supportedLocale(
            equivalentTo: requestedLocale
        ) else {
            return nil
        }

        let transcriber = DictationTranscriber(
            locale: locale,
            preset: .shortDictation
        )
        try await ensureAssets(for: [transcriber])

        let audioFile = try AVAudioFile(forReading: fileURL)
        async let transcriptTask: String = {
            var output = ""
            for try await result in transcriber.results {
                if result.isFinal {
                    output += String(result.text.characters)
                }
            }
            return output
        }()

        let analyzer = SpeechAnalyzer(modules: [transcriber])
        if let lastSample = try await analyzer.analyzeSequence(from: audioFile) {
            try await analyzer.finalizeAndFinish(through: lastSample)
        } else {
            await analyzer.cancelAndFinishNow()
        }

        return try await transcriptTask
    }

    @available(iOS 26.0, *)
    private static func ensureAssets(
        for modules: [any SpeechModule]
    ) async throws {
        if let request = try await AssetInventory.assetInstallationRequest(
            supporting: modules
        ) {
            try await request.downloadAndInstall()
        }
    }
}
