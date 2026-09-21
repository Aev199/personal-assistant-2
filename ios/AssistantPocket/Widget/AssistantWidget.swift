import SwiftUI
import WidgetKit

private struct DiagnosticEntry: TimelineEntry {
    let date: Date
}

private struct DiagnosticProvider: TimelineProvider {
    func placeholder(in context: Context) -> DiagnosticEntry {
        DiagnosticEntry(date: .now)
    }

    func getSnapshot(in context: Context, completion: @escaping (DiagnosticEntry) -> Void) {
        completion(DiagnosticEntry(date: .now))
    }

    func getTimeline(in context: Context, completion: @escaping (Timeline<DiagnosticEntry>) -> Void) {
        let entry = DiagnosticEntry(date: .now)
        completion(Timeline(entries: [entry], policy: .after(Date().addingTimeInterval(15 * 60))))
    }
}

private struct DiagnosticWidgetView: View {
    let entry: DiagnosticEntry

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label("Assistant", systemImage: "checkmark.circle.fill")
                .font(.headline)

            Text("Виджет запущен")
                .font(.title3.weight(.semibold))

            Text("Статический тест без сети и App Intents")
                .font(.caption)
                .foregroundStyle(.secondary)

            Spacer(minLength: 0)

            Text(entry.date, style: .time)
                .font(.caption2)
                .foregroundStyle(.tertiary)
        }
        .padding(12)
        .containerBackground(.fill.tertiary, for: .widget)
    }
}

struct AssistantPocketWidget: Widget {
    let kind = "AssistantPocketWidget"

    var body: some WidgetConfiguration {
        StaticConfiguration(kind: kind, provider: DiagnosticProvider()) { entry in
            DiagnosticWidgetView(entry: entry)
        }
        .configurationDisplayName("Assistant — тест")
        .description("Проверка запуска Widget Extension после sideload-подписи.")
        .supportedFamilies([.systemMedium, .systemLarge])
        .contentMarginsDisabled()
    }
}

@main
struct AssistantPocketWidgetBundle: WidgetBundle {
    @WidgetBundleBuilder
    var body: some Widget {
        AssistantPocketWidget()
    }
}
