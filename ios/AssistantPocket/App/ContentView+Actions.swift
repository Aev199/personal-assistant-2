import SwiftUI
import WidgetKit

/// Actions on tasks, reminders and meetings from the Today screen.
extension ContentView {
    @MainActor
    func confirmPending(_ pending: NativeIntakePending) async {
        errorMessage = nil
        do {
            let client = APIClient(baseURL: settings.normalizedBaseURL, token: settings.token)
            let response = try await client.confirmIntake(pendingActionID: pending.pendingActionId)
            guard response.ok else {
                throw APIClientError.invalidResponse
            }
            withAnimation {
                pendingIntake.removeAll { $0.id == pending.id }
            }
            presentConfirmation("Добавлено")
            WidgetCenter.shared.reloadTimelines(ofKind: "AssistantPocketWidget")
            await loadToday()
        } catch let APIClientError.http(code, _) where code == 409 {
            await loadPendingIntake()
            presentConfirmation("Эта запись уже обработана")
        } catch {
            presentActionError(error)
        }
    }

    @MainActor
    func cancelPending(_ pending: NativeIntakePending) async {
        errorMessage = nil
        do {
            let client = APIClient(baseURL: settings.normalizedBaseURL, token: settings.token)
            _ = try await client.cancelIntake(pendingActionID: pending.pendingActionId)
            withAnimation {
                pendingIntake.removeAll { $0.id == pending.id }
            }
        } catch let APIClientError.http(code, _) where code == 409 {
            await loadPendingIntake()
            presentConfirmation("Эта запись уже обработана")
        } catch {
            presentActionError(error)
        }
    }

    @MainActor
    func acknowledge(_ reminder: TodayReminder) async {
        errorMessage = nil
        do {
            let client = APIClient(baseURL: settings.normalizedBaseURL, token: settings.token)
            _ = try await client.acknowledgeReminder(reminderID: reminder.id)
            withAnimation {
                reminders.removeAll { $0.id == reminder.id }
            }
            presentConfirmation("Напоминание закрыто")
            WidgetCenter.shared.reloadTimelines(ofKind: "AssistantPocketWidget")
        } catch {
            presentActionError(error)
        }
    }

    @MainActor
    func snooze(_ reminder: TodayReminder) async {
        errorMessage = nil
        do {
            let client = APIClient(baseURL: settings.normalizedBaseURL, token: settings.token)
            _ = try await client.snoozeReminder(reminderID: reminder.id, minutes: 15)
            withAnimation {
                reminders.removeAll { $0.id == reminder.id }
            }
            presentConfirmation("Отложено на 15 минут")
            WidgetCenter.shared.reloadTimelines(ofKind: "AssistantPocketWidget")
        } catch {
            presentActionError(error)
        }
    }

    @MainActor
    func dismiss(_ event: TodayEvent) async {
        errorMessage = nil
        do {
            let client = APIClient(baseURL: settings.normalizedBaseURL, token: settings.token)
            _ = try await client.dismissEvent(eventID: event.id, until: event.end)
            withAnimation {
                events.removeAll { $0.id == event.id }
            }
            WidgetCenter.shared.reloadTimelines(ofKind: "AssistantPocketWidget")
        } catch {
            presentActionError(error)
        }
    }

    @MainActor
    func setPlanned(_ task: TodayTask, _ planned: Bool) async {
        errorMessage = nil
        do {
            let client = APIClient(baseURL: settings.normalizedBaseURL, token: settings.token)
            if planned {
                _ = try await client.planTask(taskID: task.id)
            } else {
                _ = try await client.unplanTask(taskID: task.id)
            }
            onChanged()
            await loadToday()
            WidgetCenter.shared.reloadTimelines(ofKind: "AssistantPocketWidget")
        } catch {
            presentActionError(error)
        }
    }

    @MainActor
    func complete(_ task: TodayTask) async {
        errorMessage = nil
        do {
            let client = APIClient(baseURL: settings.normalizedBaseURL, token: settings.token)
            _ = try await client.markDone(taskID: task.id)
            withAnimation {
                tasks.removeAll { $0.id == task.id }
            }
            onChanged()
            WidgetCenter.shared.reloadTimelines(ofKind: "AssistantPocketWidget")
        } catch {
            presentActionError(error)
        }
    }
}
