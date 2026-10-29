import Combine
import SwiftUI
import WidgetKit

struct ContentView: View {
    let refreshToken: Int
    let onChanged: () -> Void

    @EnvironmentObject var settings: AppSettings
    @Environment(\.scenePhase) var scenePhase

    @AppStorage("assistant.captureDraft") var captureText = ""
    @State var tasks: [TodayTask] = []
    @State var reminders: [TodayReminder] = []
    @State var events: [TodayEvent] = []
    @State var calendarUnavailable = false
    @State var calendarPending = false
    @State var didRetryPendingCalendar = false
    @State var hasLoadedToday = false
    @State var todayStale = false
    @State var todayLoadGeneration = 0
    @State var isLoading = false
    @State var isSending = false
    @State var errorMessage: String?
    @State var actionError: String?
    @State var confirmation: String?
    @State var confirmationRevision = 0
    @State var showSettings = false
    @AppStorage("assistant.clarificationContext") var clarificationContext = ""
    @AppStorage("assistant.clarificationPrompt") var clarificationPrompt = ""
    @State var pendingIntake: [NativeIntakePending] = []
    @State var isFlushingOutbox = false
    @State var isFlushingVoiceOutbox = false
    @State var isVoiceSending = false
    @StateObject var voiceRecorder = VoiceRecorder()
    @State var editingTask: TodayTask?
    @State var showPlanPicker = false
    @State var now = Date()
    @FocusState var captureFocused: Bool

    let clock = Timer.publish(every: 30, on: .main, in: .common).autoconnect()

    var activeEvent: TodayEvent? {
        events.first { $0.start <= now && $0.end > now }
    }

    var dueReminder: TodayReminder? {
        reminders.first(where: { reminder in
            guard let at = reminder.at else { return false }
            return at <= now
        })
    }

    var upcomingEvent: TodayEvent? {
        guard activeEvent == nil, dueReminder == nil else { return nil }
        let cutoff = now.addingTimeInterval(15 * 60)
        return events.first { $0.start > now && $0.start <= cutoff && $0.end > now }
    }

    /// A meeting in progress or starting soon, or a due reminder, is shown
    /// above «На сегодня» without changing the plan itself.
    var attentionEvent: TodayEvent? {
        activeEvent ?? upcomingEvent
    }

    var attentionReminder: TodayReminder? {
        guard activeEvent == nil else { return nil }
        return dueReminder
    }

    var planTasks: [TodayTask] {
        tasks.filter { $0.isInTodayPlan }
    }

    var suggestedTasks: [TodayTask] {
        Array(tasks.filter { !$0.isInTodayPlan }.prefix(3))
    }

    var laterEvents: [TodayEvent] {
        events.filter { $0.end > now && $0.id != attentionEvent?.id }
    }

    var laterReminders: [TodayReminder] {
        reminders.filter { $0.id != attentionReminder?.id }
    }

    var nextEvent: TodayEvent? {
        laterEvents.first
    }

    var nextReminder: TodayReminder? {
        laterReminders.first
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 26) {
                    attentionSection
                    planSection
                    dataFreshnessWarning
                    calendarWarning
                    laterSection
                    captureSection

                    if let confirmation {
                        Label(confirmation, systemImage: "checkmark.circle.fill")
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                            .transition(.opacity)
                    }

                    if hasLoadedToday, let errorMessage {
                        Label(errorMessage, systemImage: "wifi.exclamationmark")
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                    }
                }
                .padding(.horizontal, 20)
                .padding(.vertical, 18)
            }
            .navigationTitle("Сегодня")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button {
                        showSettings = true
                    } label: {
                        Image(systemName: "gearshape")
                    }
                    .accessibilityLabel("Настройки")
                }
            }
            .refreshable {
                await loadToday()
            }
            .task {
                // A widget/deep-link capture is the user's foreground intent.
                // Start it before any refresh or retry work on a cold launch.
                consumeSystemCaptureRequest()

                if settings.isConfigured {
                    await loadToday()
                    await loadPendingIntake()
                    await flushOutbox()
                    await flushVoiceOutbox()
                } else {
                    showSettings = true
                }
            }
            .onReceive(NotificationCenter.default.publisher(for: CaptureLaunchSignal.notification)) { notification in
                let mode = CaptureLaunchSignal.mode(from: notification)
                _ = CaptureLaunchSignal.consumeMode()
                activateCapture(mode: mode)
            }
            .onReceive(clock) { tick in
                now = tick
            }
            .onChange(of: scenePhase) { phase in
                if phase == .active {
                    didRetryPendingCalendar = false
                    consumeSystemCaptureRequest()
                    Task {
                        // AppRoot bumps refreshToken for all primary tabs on foreground.
                        // Keep recovery work here, but do not issue a duplicate /today request.
                        await loadPendingIntake()
                        await flushOutbox()
                        await flushVoiceOutbox()
                    }
                } else if voiceRecorder.isRecording {
                    persistInterruptedVoiceCapture()
                }
            }
            .onChange(of: refreshToken) { _, _ in
                Task { await loadToday() }
            }
            .alert("Не удалось выполнить действие", isPresented: actionErrorPresented) {
                Button("OK", role: .cancel) {}
            } message: {
                Text(actionError ?? "")
            }
            .sheet(isPresented: $showSettings, onDismiss: {
                Task { await loadToday() }
            }) {
                SettingsView()
                    .environmentObject(settings)
            }
            .sheet(isPresented: $showPlanPicker, onDismiss: {
                Task { await loadToday() }
            }) {
                PlanPickerView {
                    onChanged()
                    WidgetCenter.shared.reloadTimelines(ofKind: "AssistantPocketWidget")
                }
                .environmentObject(settings)
            }
            .sheet(item: $editingTask) { task in
                TaskEditView(task: task) {
                    onChanged()
                    Task { await loadToday() }
                }
                .environmentObject(settings)
            }
        }
    }


    @MainActor
    func loadToday() async {
        guard settings.isConfigured else { return }

        todayLoadGeneration += 1
        let generation = todayLoadGeneration
        isLoading = true
        errorMessage = nil
        defer {
            if generation == todayLoadGeneration {
                isLoading = false
            }
        }

        do {
            let client = APIClient(baseURL: settings.normalizedBaseURL, token: settings.token)
            let response = try await client.loadToday()
            guard generation == todayLoadGeneration else { return }

            tasks = response.tasks
            reminders = response.reminders
            events = response.events ?? []
            calendarUnavailable = response.calendarUnavailable ?? false
            calendarPending = response.calendarPending ?? false
            hasLoadedToday = true
            todayStale = false

            if calendarPending && !didRetryPendingCalendar {
                didRetryPendingCalendar = true
                DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
                    Task { await loadToday() }
                }
            } else if !calendarPending {
                didRetryPendingCalendar = false
            }
        } catch {
            guard generation == todayLoadGeneration else { return }
            if hasLoadedToday {
                todayStale = true
            } else {
                present(error)
            }
        }
    }

    @MainActor
    func loadPendingIntake() async {
        guard settings.isConfigured else { return }
        do {
            let client = APIClient(baseURL: settings.normalizedBaseURL, token: settings.token)
            pendingIntake = try await client.loadPendingIntake().pending
        } catch {
            // A background restore failure should not interrupt the Today surface.
        }
    }


    @MainActor
    func presentConfirmation(_ message: String) {
        confirmationRevision += 1
        let revision = confirmationRevision
        withAnimation(.easeOut(duration: 0.15)) {
            confirmation = message
        }

        DispatchQueue.main.asyncAfter(deadline: .now() + 2.4) {
            guard confirmationRevision == revision else { return }
            withAnimation(.easeIn(duration: 0.15)) {
                confirmation = nil
            }
        }
    }

    var actionErrorPresented: Binding<Bool> {
        Binding(
            get: { actionError != nil },
            set: { isPresented in
                if !isPresented {
                    actionError = nil
                }
            }
        )
    }

    @MainActor
    func presentActionError(_ error: Error) {
        if error is CancellationError {
            return
        }
        if let urlError = error as? URLError {
            switch urlError.code {
            case .cancelled:
                return
            case .timedOut, .notConnectedToInternet, .networkConnectionLost,
                 .cannotConnectToHost, .cannotFindHost, .dnsLookupFailed:
                actionError = "Нет связи с Assistant."
                return
            default:
                break
            }
        }
        actionError = error.localizedDescription
    }

    @MainActor
    func present(_ error: Error) {
        if error is CancellationError {
            return
        }
        if let urlError = error as? URLError {
            switch urlError.code {
            case .cancelled:
                return
            case .timedOut, .notConnectedToInternet, .networkConnectionLost, .cannotConnectToHost, .cannotFindHost:
                errorMessage = "Нет связи с Assistant."
                return
            default:
                break
            }
        }
        errorMessage = error.localizedDescription
    }
}
