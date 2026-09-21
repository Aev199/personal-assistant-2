import AppIntents
import SwiftUI
import WidgetKit

struct DiagnosticWidgetConfigurationIntent: WidgetConfigurationIntent {
    static var title: LocalizedStringResource = "Assistant"
    static var description = IntentDescription("Диагностика configurable widget")

    @Parameter(title: "Адрес Assistant")
    var serverURL: String?

    @Parameter(title: "Код виджета")
    var token: String?
}

private struct DiagnosticEntry: TimelineEntry {
    let date: Date
    let configuration: DiagnosticWidgetConfigurationIntent
}

private struct DiagnosticProvider: AppIntentTimelineProvider {
    func placeholder(in context: Context) -> DiagnosticEntry {
        DiagnosticEntry(
            date: .now,
            configuration: DiagnosticWidgetConfigurationIntent()
        )
    }

    func snapshot(
        for configuration: DiagnosticWidgetConfigurationIntent,
        in context: Context
    ) async -> DiagnosticEntry {
        DiagnosticEntry(date: .now, configuration: configuration)
    }

    func timeline(
        for configuration: DiagnosticWidgetConfigurationIntent,
        in context: Context
    ) async -> Timeline<DiagnosticEntry> {
        let entry = DiagnosticEntry(date: .now, configuration: configuration)
        return Timeline(
            entries: [entry],
            policy: .after(Date().addingTimeInterval(15 * 60))
        )
    }
}

private struct DiagnosticWidgetView: View {
    let entry: DiagnosticEntry

    private var configured: Bool {
        let server = (entry.configuration.serverURL ?? "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        let token = (entry.configuration.token ?? "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return !server.isEmpty && !token.isEmpty
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label("Assistant", systemImage: "gearshape.fill")
                .font(.headline)

            Text("AppIntentConfiguration запущен")
                .font(.title3.weight(.semibold))

            Text(configured ? "Поля настройки получены" : "Зажмите → Изменить виджет")
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
        AppIntentConfiguration(
            kind: kind,
            intent: DiagnosticWidgetConfigurationIntent.self,
            provider: DiagnosticProvider()
        ) { entry in
            DiagnosticWidgetView(entry: entry)
        }
        .configurationDisplayName("Assistant — тест AppIntent")
        .description("Проверка AppIntentConfiguration после sideload-подписи.")
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
