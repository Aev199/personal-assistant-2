import SwiftUI
import WidgetKit

/// Capture: text and voice, durable outboxes, one clarification, calendar confirmation.
extension ContentView {
    var captureSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Запомнить")
                .font(.headline)

            HStack(alignment: .bottom, spacing: 10) {
                TextField(
                    voiceRecorder.isRecording
                        ? "Говорите…"
                        : (clarificationPrompt.isEmpty ? "Написать или надиктовать…" : "Уточнить…"),
                    text: $captureText,
                    axis: .vertical
                )
                    .lineLimit(1...4)
                    .textFieldStyle(.plain)
                    .focused($captureFocused)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 12)
                    .background(.quaternary, in: RoundedRectangle(cornerRadius: 14))
                    .submitLabel(.send)
                    .onSubmit {
                        Task { await capture() }
                    }
                    .disabled(voiceRecorder.isRecording || isVoiceSending)

                Button {
                    Task {
                        if voiceRecorder.isRecording {
                            await finishVoiceCapture()
                        } else {
                            await startVoiceCapture()
                        }
                    }
                } label: {
                    if isVoiceSending {
                        ProgressView()
                            .controlSize(.small)
                            .frame(width: 22, height: 22)
                    } else {
                        Image(systemName: voiceRecorder.isRecording ? "stop.fill" : "mic.fill")
                            .font(.headline)
                    }
                }
                .buttonStyle(.bordered)
                .buttonBorderShape(.circle)
                .disabled(isSending || isVoiceSending)
                .accessibilityLabel(voiceRecorder.isRecording ? "Остановить и отправить" : "Записать голосом")

                Button {
                    Task { await capture() }
                } label: {
                    if isSending {
                        ProgressView()
                            .controlSize(.small)
                            .frame(width: 22, height: 22)
                    } else {
                        Image(systemName: "arrow.up")
                            .font(.headline)
                    }
                }
                .buttonStyle(.borderedProminent)
                .buttonBorderShape(.circle)
                .disabled(
                    captureText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                    || isSending
                    || isVoiceSending
                    || voiceRecorder.isRecording
                )
                .accessibilityLabel("Сохранить")
            }

            if !clarificationPrompt.isEmpty {
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: "questionmark.circle")
                        .foregroundStyle(.secondary)

                    Text(clarificationPrompt)
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)

                    Spacer(minLength: 6)

                    Button {
                        clearClarification()
                    } label: {
                        Image(systemName: "xmark.circle.fill")
                            .foregroundStyle(.secondary)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Отменить уточнение")
                }
            }

            ForEach(pendingIntake) { pending in
                VStack(alignment: .leading, spacing: 10) {
                    Text(pending.title)
                        .font(.subheadline.weight(.semibold))
                        .fixedSize(horizontal: false, vertical: true)

                    if let start = pending.payload?.startLocal {
                        HStack(spacing: 6) {
                            Text(start, format: .dateTime.day().month().hour().minute())
                            if let duration = pending.payload?.durationMin {
                                Text("·")
                                Text("\(duration) мин")
                            }
                        }
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    }

                    HStack(spacing: 10) {
                        Button("Добавить") {
                            Task { await confirmPending(pending) }
                        }
                        .buttonStyle(.borderedProminent)

                        Button("Не добавлять") {
                            Task { await cancelPending(pending) }
                        }
                        .buttonStyle(.bordered)
                    }
                }
                .padding(12)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(.quaternary, in: RoundedRectangle(cornerRadius: 14))
            }
        }
    }

    func consumeSystemCaptureRequest() {
        guard let mode = CaptureLaunchSignal.consumeMode() else { return }
        activateCapture(mode: mode)
    }

    func activateCapture(mode: CaptureLaunchMode) {
        // Capture launched from a widget/deep link means "new thought", not
        // "answer the clarification I happened to leave open earlier".
        clearClarification()

        if mode == .voice {
            activateVoiceCapture()
        } else {
            activateCapture()
        }
    }

    func activateVoiceCapture() {
        confirmation = nil
        errorMessage = nil
        guard settings.isConfigured else {
            showSettings = true
            return
        }
        captureFocused = false
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.18) {
            Task { await startVoiceCapture() }
        }
    }

    func activateCapture() {
        confirmation = nil
        errorMessage = nil
        guard settings.isConfigured else {
            showSettings = true
            return
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.12) {
            captureFocused = true
        }
    }


    @MainActor
    func persistInterruptedVoiceCapture() {
        guard let recordingURL = voiceRecorder.stop() else { return }
        let context = clarificationContext.isEmpty ? nil : clarificationContext

        do {
            _ = try VoiceCaptureOutbox.enqueue(
                recordingURL: recordingURL,
                context: context
            )
        } catch {
            // Keep the temporary recording rather than deleting a capture
            // that failed to move into the durable outbox.
        }
    }

    @MainActor
    func startVoiceCapture() async {
        guard settings.isConfigured else {
            showSettings = true
            return
        }
        guard !voiceRecorder.isRecording, !isVoiceSending else { return }

        confirmation = nil
        errorMessage = nil
        captureFocused = false

        do {
            try await voiceRecorder.start()
            Task {
                await LocalSpeechTranscriber.preparePreferredAssets()
            }
        } catch {
            presentActionError(error)
        }
    }

    @MainActor
    func finishVoiceCapture() async {
        guard let recordingURL = voiceRecorder.stop() else { return }
        let context = clarificationContext.isEmpty ? nil : clarificationContext

        do {
            let queued = try VoiceCaptureOutbox.enqueue(
                recordingURL: recordingURL,
                context: context
            )
            await sendVoiceCapture(queued)
        } catch {
            // If moving into the durable outbox failed, leave the temporary
            // recording in place rather than deleting the only audio copy.
            presentActionError(error)
        }
    }

    @MainActor
    func sendVoiceCapture(_ queued: QueuedVoiceCapture) async {
        guard settings.isConfigured else { return }

        isVoiceSending = true
        errorMessage = nil
        confirmation = nil
        defer { isVoiceSending = false }

        if let existingText = CaptureOutbox.item(id: queued.id) {
            await sendTranscribedVoiceCapture(existingText)
            return
        }

        do {
            if let textCapture = try await makeLocalVoiceTextCapture(queued) {
                await sendTranscribedVoiceCapture(textCapture)
                return
            }

            try Task.checkCancellation()
            let audioData = try VoiceCaptureOutbox.data(for: queued)
            let client = APIClient(
                baseURL: settings.normalizedBaseURL,
                token: settings.token
            )
            let response = try await client.voiceIntake(
                audioData: audioData,
                context: queued.context,
                clientID: queued.id
            )

            if response.status == "stored" {
                if let transcript = response.transcript?.trimmingCharacters(in: .whitespacesAndNewlines),
                   !transcript.isEmpty {
                    _ = CaptureOutbox.enqueue(
                        text: transcript,
                        context: queued.context,
                        id: queued.id
                    )
                    VoiceCaptureOutbox.remove(queued.id)
                }
                presentConfirmation("Голос записан, разберу позже")
                return
            }

            VoiceCaptureOutbox.remove(queued.id)
            await applyIntakeResponse(
                response,
                originalText: response.transcript ?? "Голосовая запись"
            )
        } catch is CancellationError {
            // Keep the durable audio. Cancellation must never silently turn
            // into an upload to the server.
            return
        } catch {
            if isRetryable(error) {
                presentConfirmation("Голос сохранён на телефоне")
                return
            }

            // The audio stays durable if both the local and server paths fail.
            presentActionError(error)
        }
    }

    @MainActor
    func makeLocalVoiceTextCapture(
        _ queued: QueuedVoiceCapture
    ) async throws -> QueuedCapture? {
        let transcript = try await LocalSpeechTranscriber.transcribe(
            fileURL: VoiceCaptureOutbox.url(for: queued)
        )
        guard let transcript,
              !transcript.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            return nil
        }

        return CaptureOutbox.enqueue(
            text: transcript,
            context: queued.context,
            id: queued.id
        )
    }

    @MainActor
    func sendTranscribedVoiceCapture(
        _ queued: QueuedCapture,
        mayResolveClarification: Bool = true
    ) async {
        do {
            let client = APIClient(baseURL: settings.normalizedBaseURL, token: settings.token)
            let response = try await client.intake(
                queued.text,
                context: queued.context,
                clientID: queued.id
            )

            if response.status == "stored" {
                VoiceCaptureOutbox.remove(queued.id)
                presentConfirmation("Голос распознан, разберу позже")
                return
            }

            CaptureOutbox.remove(queued.id)
            VoiceCaptureOutbox.remove(queued.id)
            await applyIntakeResponse(
                response,
                originalText: queued.text,
                mayResolveClarification: mayResolveClarification
            )
        } catch {
            // The transcript is already durable in CaptureOutbox. Keep it for
            // any failure so a successful local recognition can never be lost.
            if isRetryable(error) {
                presentConfirmation("Голос распознан и сохранён на телефоне")
            } else {
                presentActionError(error)
            }
        }
    }

    @MainActor
    func flushVoiceOutbox() async {
        guard settings.isConfigured, !isFlushingVoiceOutbox, !voiceRecorder.isRecording else {
            return
        }

        VoiceCaptureOutbox.recoverTemporaryRecordings()
        let queued = VoiceCaptureOutbox.all()
        guard !queued.isEmpty else { return }

        isFlushingVoiceOutbox = true
        defer { isFlushingVoiceOutbox = false }

        let client = APIClient(
            baseURL: settings.normalizedBaseURL,
            token: settings.token
        )

        for item in queued {
            if let existingText = CaptureOutbox.item(id: item.id) {
                await sendTranscribedVoiceCapture(
                    existingText,
                    mayResolveClarification: item.context != nil
                        && item.context == clarificationContext
                )
                continue
            }

            do {
                if let textCapture = try await makeLocalVoiceTextCapture(item) {
                    await sendTranscribedVoiceCapture(
                        textCapture,
                        mayResolveClarification: item.context != nil
                            && item.context == clarificationContext
                    )
                    continue
                }

                try Task.checkCancellation()
                let response = try await client.voiceIntake(
                    audioData: VoiceCaptureOutbox.data(for: item),
                    context: item.context,
                    clientID: item.id
                )
                if response.status == "stored" {
                    if let transcript = response.transcript?.trimmingCharacters(in: .whitespacesAndNewlines),
                       !transcript.isEmpty {
                        _ = CaptureOutbox.enqueue(
                            text: transcript,
                            context: item.context,
                            id: item.id
                        )
                        VoiceCaptureOutbox.remove(item.id)
                    }
                    continue
                }

                VoiceCaptureOutbox.remove(item.id)
                await applyIntakeResponse(
                    response,
                    originalText: response.transcript ?? "Голосовая запись",
                    mayResolveClarification: item.context != nil
                        && item.context == clarificationContext
                )
            } catch is CancellationError {
                break
            } catch {
                if isRetryable(error) {
                    break
                }
                // Preserve this item for inspection/retry, but let later
                // captures continue instead of poisoning the whole queue.
                continue
            }
        }
    }

    @MainActor
    func capture() async {
        let text = captureText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        guard settings.isConfigured else {
            showSettings = true
            return
        }

        isSending = true
        errorMessage = nil
        confirmation = nil
        let context = clarificationContext.isEmpty ? nil : clarificationContext
        let queued = CaptureOutbox.enqueue(text: text, context: context)
        defer { isSending = false }

        do {
            let client = APIClient(baseURL: settings.normalizedBaseURL, token: settings.token)
            let response = try await client.intake(text, context: context, clientID: queued.id)
            captureText = ""

            if response.status == "stored" {
                captureFocused = false
                presentConfirmation("Сохранено, разберу позже")
                return
            }

            CaptureOutbox.remove(queued.id)
            await applyIntakeResponse(response, originalText: text)
        } catch {
            captureText = ""
            captureFocused = false

            if isRetryable(error) {
                presentConfirmation("Сохранено на телефоне")
            } else {
                actionError = "Не удалось отправить. Запись сохранена на телефоне."
            }
        }
    }

    @MainActor
    func applyIntakeResponse(
        _ response: NativeIntakeResponse,
        originalText: String,
        mayResolveClarification: Bool = true
    ) async {
        if let need = response.needsInput.first {
            clarificationPrompt = need.prompt
            clarificationContext = response.context ?? (clarificationContext.isEmpty ? originalText : clarificationContext)
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.08) {
                captureFocused = true
            }
        } else if mayResolveClarification {
            clearClarification()
        }

        for pending in response.pending where !pendingIntake.contains(where: { $0.id == pending.id }) {
            pendingIntake.append(pending)
        }

        let savedCount = response.saved.count
        if response.inboxFallback == true {
            presentConfirmation("Записано во Входящие")
        } else if savedCount == 1, let item = response.saved.first {
            switch item.kind.lowercased() {
            case "idea":
                presentConfirmation("Идея сохранена")
            case "reminder":
                presentConfirmation("Напоминание создано")
            case "event", "calendar_event":
                presentConfirmation("Встреча добавлена")
            default:
                presentConfirmation("Задача записана")
            }
        } else if savedCount > 1 {
            presentConfirmation("Записано: \(savedCount)")
        } else if response.status == "stored" {
            presentConfirmation(response.message ?? "Записано")
        }

        if savedCount > 0 {
            onChanged()
            WidgetCenter.shared.reloadTimelines(ofKind: "AssistantPocketWidget")
            await loadToday()
        }
    }

    @MainActor
    func clearClarification() {
        clarificationPrompt = ""
        clarificationContext = ""
        captureFocused = false
    }

    @MainActor
    func flushOutbox() async {
        guard settings.isConfigured, !isFlushingOutbox else { return }
        let queued = CaptureOutbox.all()
        guard !queued.isEmpty else { return }

        isFlushingOutbox = true
        defer { isFlushingOutbox = false }

        let client = APIClient(baseURL: settings.normalizedBaseURL, token: settings.token)
        var refreshed = false

        for item in queued {
            do {
                let response = try await client.intake(item.text, context: item.context, clientID: item.id)
                if response.status == "stored" {
                    VoiceCaptureOutbox.remove(item.id)
                    presentConfirmation("Сохранено, разберу позже")
                    break
                }

                CaptureOutbox.remove(item.id)
                VoiceCaptureOutbox.remove(item.id)
                await applyIntakeResponse(
                    response,
                    originalText: item.text,
                    mayResolveClarification: item.context != nil
                        && item.context == clarificationContext
                )
                refreshed = refreshed || !response.saved.isEmpty
            } catch {
                if isRetryable(error) {
                    break
                }
                // Keep the bad item, but do not let it permanently block every
                // newer capture behind it.
                continue
            }
        }

        if refreshed {
            WidgetCenter.shared.reloadTimelines(ofKind: "AssistantPocketWidget")
        }
    }

    func isRetryable(_ error: Error) -> Bool {
        if let urlError = error as? URLError {
            switch urlError.code {
            case .timedOut, .notConnectedToInternet, .networkConnectionLost,
                 .cannotConnectToHost, .cannotFindHost, .dnsLookupFailed:
                return true
            default:
                return false
            }
        }
        if let apiError = error as? APIClientError,
           case let .http(code, _) = apiError {
            return code == 502 || code == 503 || code == 504
        }
        return false
    }
}
