import Foundation

func taskDeadlineText(_ deadline: Date, relativeTo now: Date = .now) -> String {
    let calendar = Calendar.current
    let locale = Locale(identifier: "ru_RU")
    let time = deadline.formatted(
        .dateTime.hour().minute().locale(locale)
    )

    if calendar.isDate(deadline, inSameDayAs: now) {
        return "сегодня \(time)"
    }

    if let tomorrow = calendar.date(byAdding: .day, value: 1, to: now),
       calendar.isDate(deadline, inSameDayAs: tomorrow) {
        return "завтра \(time)"
    }

    if let yesterday = calendar.date(byAdding: .day, value: -1, to: now),
       calendar.isDate(deadline, inSameDayAs: yesterday) {
        return "вчера \(time)"
    }

    let sameYear =
        calendar.component(.year, from: deadline)
        == calendar.component(.year, from: now)

    let date: String
    if sameYear {
        date = deadline.formatted(
            .dateTime.day().month(.abbreviated).locale(locale)
        )
    } else {
        date = deadline.formatted(
            .dateTime.day().month(.abbreviated).year().locale(locale)
        )
    }

    return "\(date) \(time)"
}
