import AVFoundation
import Foundation
import Speech

enum LocalSpeechTranscriber {
    private static let preferredLocales = [
        Locale(identifier: "ru-RU"),
        Locale.current,
    ]

    static func preparePreferredAssets() async {
        for requestedLocale in preferredLocales {
            if SpeechTranscriber.isAvailable,
               let locale = await SpeechTranscriber.supportedLocale(
                   equivalentTo: requestedLocale
               ) {
                let transcriber = SpeechTranscriber(
                    locale: locale,
                    preset: .transcription
                )
                if (try? await ensureAssets(for: [transcriber])) != nil {
                    return
                }
            }
        }

        for requestedLocale in preferredLocales {
            if let locale = await DictationTranscriber.supportedLocale(
                equivalentTo: requestedLocale
            ) {
                let transcriber = DictationTranscriber(
                    locale: locale,
                    preset: .shortDictation
                )
                if (try? await ensureAssets(for: [transcriber])) != nil {
                    return
                }
            }
        }
    }

    static func transcribe(fileURL: URL) async throws -> String? {
        for requestedLocale in preferredLocales {
            try Task.checkCancellation()
            do {
                if let clean = cleaned(
                    try await transcribeWithSpeechTranscriber(
                        fileURL: fileURL,
                        requestedLocale: requestedLocale
                    )
                ) {
                    return clean
                }
            } catch is CancellationError {
                throw CancellationError()
            } catch {
                // Try the next locale, then DictationTranscriber.
            }
        }

        for requestedLocale in preferredLocales {
            try Task.checkCancellation()
            do {
                if let clean = cleaned(
                    try await transcribeWithDictationTranscriber(
                        fileURL: fileURL,
                        requestedLocale: requestedLocale
                    )
                ) {
                    return clean
                }
            } catch is CancellationError {
                throw CancellationError()
            } catch {
                // The server audio path remains the final fallback.
            }
        }

        try Task.checkCancellation()
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
            preset: .transcription
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

    @discardableResult
    private static func ensureAssets(
        for modules: [any SpeechModule]
    ) async throws -> Bool {
        if let request = try await AssetInventory.assetInstallationRequest(
            supporting: modules
        ) {
            try await request.downloadAndInstall()
        }
        return true
    }
}
