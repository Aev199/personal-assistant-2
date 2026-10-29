import SwiftUI

/// Today screen sections: meeting/reminder card, «На сегодня», «Позже сегодня».
extension ContentView {
    @ViewBuilder
    var dataFreshnessWarning: some View {
        if todayStale {
            HStack(spacing: 8) {
                Label("Не удалось обновить · показаны последние данные", systemImage: "wifi.slash")
                    .font(.caption)
                    .foregroundStyle(.secondary)

                Spacer(minLength: 8)

                Button {
                    Task { await loadToday() }
                } label: {
                    Image(systemName: "arrow.clockwise")
                }
                .buttonStyle(.plain)
                .foregroundStyle(.secondary)
                .accessibilityLabel("Повторить обновление")
            }
        }
    }

    @ViewBuilder
    var calendarWarning: some View {
        // A slow calendar is retried quietly; only a real failure is shown,
        // so an unknown calendar never looks like a free day.
        if calendarUnavailable && !calendarPending {
            HStack(spacing: 8) {
                Label("Календарь не обновился", systemImage: "calendar.badge.exclamationmark")
                    .font(.caption)
                    .foregroundStyle(.secondary)

                Spacer(minLength: 8)

                Button {
                    Task { await loadToday() }
                } label: {
                    Image(systemName: "arrow.clockwise")
                }
                .buttonStyle(.plain)
                .foregroundStyle(.secondary)
                .accessibilityLabel("Повторить обновление календаря")
            }
        }
    }

    @ViewBuilder
    var attentionSection: some View {
        if let event = attentionEvent {
            eventFocusCard(event)
        } else if let reminder = attentionReminder {
            reminderFocusCard(reminder)
        }
    }

    @ViewBuilder
    var planSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("На сегодня")
                    .font(.headline)

                Spacer()

                if isLoading {
                    ProgressView()
                        .controlSize(.small)
                }

                if hasLoadedToday {
                    Button("Добавить") {
                        showPlanPicker = true
                    }
                    .font(.subheadline.weight(.medium))
                    .accessibilityLabel("Добавить дела на сегодня")
                }
            }

            if !hasLoadedToday && errorMessage != nil && settings.isConfigured && !isLoading {
                VStack(alignment: .leading, spacing: 10) {
                    Label("Не удалось обновить", systemImage: "wifi.exclamationmark")
                        .font(.title3.weight(.semibold))

                    Text("Проверьте соединение или VPN.")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)

                    Button("Повторить") {
                        Task { await loadToday() }
                    }
                    .buttonStyle(.bordered)
                }
                .padding(16)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(.quaternary, in: RoundedRectangle(cornerRadius: 16))
            } else if !settings.isConfigured {
                Button("Подключить Assistant") {
                    showSettings = true
                }
                .buttonStyle(.borderedProminent)
            } else if !planTasks.isEmpty {
                ForEach(planTasks) { task in
                    planRow(task)
                }
            } else if hasLoadedToday {
                VStack(alignment: .leading, spacing: 8) {
                    Text("Пока ничего не выбрано")
                        .font(.title3.weight(.semibold))

                    if !suggestedTasks.isEmpty {
                        Text("Добавьте 2–3 дела:")
                            .font(.subheadline)
                            .foregroundStyle(.secondary)

                        ForEach(suggestedTasks) { task in
                            suggestionRow(task)
                        }
                    }
                }
            }
        }
    }

    func planRow(_ task: TodayTask) -> some View {
        HStack(alignment: .top, spacing: 11) {
            Button {
                Task { await complete(task) }
            } label: {
                Image(systemName: "circle")
                    .font(.title3)
                    .padding(.top, 1)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Выполнено: \(task.title)")

            VStack(alignment: .leading, spacing: 4) {
                Text(task.title)
                    .font(.body)
                    .fixedSize(horizontal: false, vertical: true)

                taskMeta(task)
            }
            .contentShape(Rectangle())
            .onTapGesture {
                editingTask = task
            }

            Spacer(minLength: 0)

            Button {
                Task { await setPlanned(task, false) }
            } label: {
                Image(systemName: "minus.circle")
                    .font(.body)
                    .foregroundStyle(.secondary)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Убрать из «На сегодня»: \(task.title)")
        }
        .padding(.vertical, 5)
    }

    func suggestionRow(_ task: TodayTask) -> some View {
        HStack(alignment: .top, spacing: 11) {
            VStack(alignment: .leading, spacing: 4) {
                Text(task.title)
                    .font(.body)
                    .lineLimit(2)

                taskMeta(task)
            }
            .contentShape(Rectangle())
            .onTapGesture {
                editingTask = task
            }

            Spacer(minLength: 0)

            Button("На сегодня") {
                Task { await setPlanned(task, true) }
            }
            .buttonStyle(.bordered)
            .controlSize(.small)
            .accessibilityLabel("Добавить на сегодня: \(task.title)")
        }
        .padding(.vertical, 5)
    }

    func eventFocusCard(_ event: TodayEvent) -> some View {
        HStack(alignment: .top, spacing: 12) {
            VStack(alignment: .leading, spacing: 7) {
                Label("Календарь", systemImage: "calendar")
                    .font(.caption.weight(.medium))
                    .foregroundStyle(.secondary)
                Text(event.title)
                    .font(.title3.weight(.semibold))
                    .fixedSize(horizontal: false, vertical: true)
                Text(event.start, format: .dateTime.hour().minute())
                    + Text("–")
                    + Text(event.end, format: .dateTime.hour().minute())
            }
            .font(.subheadline)

            Spacer(minLength: 8)

            Button {
                Task { await dismiss(event) }
            } label: {
                Image(systemName: "xmark.circle.fill")
                    .font(.title3)
                    .foregroundStyle(.secondary)
                    .frame(width: 38, height: 38)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Убрать встречу из внимания")
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.quaternary, in: RoundedRectangle(cornerRadius: 16))
    }

    func reminderFocusCard(_ reminder: TodayReminder) -> some View {
        HStack(alignment: .top, spacing: 12) {
            VStack(alignment: .leading, spacing: 7) {
                Label("Напоминание", systemImage: "bell.fill")
                    .font(.caption.weight(.medium))
                    .foregroundStyle(.secondary)
                Text(reminder.text)
                    .font(.title3.weight(.semibold))
                if let at = reminder.at {
                    Text(at, format: .dateTime.hour().minute())
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }
            }

            Spacer(minLength: 0)

            VStack(spacing: 8) {
                Button("ОК") {
                    Task { await acknowledge(reminder) }
                }
                .buttonStyle(.borderedProminent)
                .buttonBorderShape(.capsule)
                .accessibilityLabel("Закрыть напоминание")

                Button("+15") {
                    Task { await snooze(reminder) }
                }
                .buttonStyle(.bordered)
                .buttonBorderShape(.capsule)
                .accessibilityLabel("Отложить на 15 минут")
            }
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.quaternary, in: RoundedRectangle(cornerRadius: 16))
    }

    @ViewBuilder
    var laterSection: some View {
        if nextEvent != nil || nextReminder != nil {
            VStack(alignment: .leading, spacing: 10) {
                Text("Позже сегодня")
                    .font(.headline)

                nextTimedRows
            }
        }
    }

    @ViewBuilder
    var nextTimedRows: some View {
        if let event = nextEvent, let reminder = nextReminder, let reminderAt = reminder.at {
            if reminderAt <= event.start {
                compactReminderRow(reminder)
                compactEventRow(event)
            } else {
                compactEventRow(event)
                compactReminderRow(reminder)
            }
        } else {
            if let event = nextEvent {
                compactEventRow(event)
            }
            if let reminder = nextReminder {
                compactReminderRow(reminder)
            }
        }
    }

    func compactEventRow(_ event: TodayEvent) -> some View {
        HStack(alignment: .top, spacing: 11) {
            Image(systemName: "calendar")
                .font(.body)
                .foregroundStyle(.secondary)
                .padding(.top, 2)

            VStack(alignment: .leading, spacing: 4) {
                Text(event.title)
                    .font(.body)
                    .lineLimit(2)
                Text(event.start, format: .dateTime.hour().minute())
                    + Text("–")
                    + Text(event.end, format: .dateTime.hour().minute())

                if let relative = timeUntilText(event.start) {
                    Text(relative)
                }
            }
            .font(.caption)
            .foregroundStyle(.secondary)

            Spacer(minLength: 0)

            Button {
                Task { await dismiss(event) }
            } label: {
                Image(systemName: "xmark.circle")
                    .font(.body)
                    .foregroundStyle(.secondary)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Убрать встречу из внимания")
        }
        .padding(.vertical, 5)
    }

    func timeUntilText(_ date: Date) -> String? {
        let seconds = date.timeIntervalSince(now)
        guard seconds > 0 else { return nil }

        let minutes = max(1, Int((seconds / 60).rounded(.up)))
        if minutes < 60 {
            return "через \(minutes) мин"
        }

        let hours = minutes / 60
        let rest = minutes % 60
        guard hours <= 2 else { return nil }
        return rest == 0 ? "через \(hours) ч" : "через \(hours) ч \(rest) мин"
    }

    func compactReminderRow(_ reminder: TodayReminder) -> some View {
        HStack(alignment: .top, spacing: 11) {
            Image(systemName: "bell")
                .font(.body)
                .foregroundStyle(.secondary)
                .padding(.top, 2)

            VStack(alignment: .leading, spacing: 4) {
                Text(reminder.text)
                    .font(.body)
                    .lineLimit(2)
                if let at = reminder.at {
                    Text(at, format: .dateTime.hour().minute())
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }

            Spacer(minLength: 0)

            Button("+15") {
                Task { await snooze(reminder) }
            }
            .buttonStyle(.plain)
            .font(.caption.weight(.medium))
            .foregroundStyle(.secondary)
            .accessibilityLabel("Отложить на 15 минут")
        }
        .padding(.vertical, 5)
    }

    @ViewBuilder
    func taskMeta(_ task: TodayTask) -> some View {
        HStack(spacing: 6) {
            if task.isPersonal {
                Text("Личное")
            } else if !task.project.isEmpty && task.project.uppercased() != "INBOX" {
                Text(task.project)
            }

            if let deadline = task.deadline {
                if task.overdue {
                    Text(taskDeadlineText(deadline))
                        .foregroundStyle(.red)
                } else {
                    Text(taskDeadlineText(deadline))
                        .foregroundStyle(.secondary)
                }
            }

            if !task.assignee.isEmpty {
                Text(task.assignee)
            }

        }
        .font(.caption)
        .foregroundStyle(.secondary)
    }
}
