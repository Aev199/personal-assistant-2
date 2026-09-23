# Assistant for iOS

The native iOS app is a primary mobile surface of Personal Assistant, not a companion to Telegram.

## Product roles

- **Widget** — the lowest-friction daily surface: see what matters now, Quick Done, refresh, text capture and one-tap voice capture.
- **iOS app** — three stable native tabs: Today/capture, Tasks, and Ideas. Calendar context stays inside Today. No chat clone and no project-management dashboard.
- **Telegram** — conversational / command interface to the same backend, especially useful while working on a PC.
- **Backend** — source of truth and owner of attention ordering, tasks, reminders and mutations.

The same Task/Reminder data is used by every client. Calendar events are supplied by the backend with a short cache; only current and upcoming timed events enter the native attention surface.

## Native UI scope

The main screen remains deliberately small:

1. **Сейчас** — one attention task or reminder.
2. **Дальше** — a short continuation, not the whole backlog.
3. **Запомнить** — fast Inbox capture.

The standard bottom tab bar contains **Сегодня**, **Задачи**, and **Идеи**. Tasks provides the complete active backlog with work/personal scopes and local search. Ideas stay outside the attention queue until explicitly promoted to a task.

The Home Screen widget uses StaticConfiguration; configurable AppIntentConfiguration is intentionally avoided because it fails after the current ESign sideload resigning flow.

## Backend API

Canonical native-client routes:

- GET /api/v1/today
- GET /api/v1/tasks?limit=100
- GET /api/v1/ideas
- POST /api/v1/ideas/{id}/promote
- POST /api/v1/ideas/{id}/archive
- POST /api/v1/capture with a JSON text field
- POST /api/v1/intake/audio with multipart AAC/M4A voice data
- POST /api/v1/tasks/{id}/done

Legacy /api/v1/companion/... routes remain as compatibility aliases. Current iOS builds try the canonical route first and fall back to legacy routes where possible, so the app can survive a rolling backend upgrade.

Authentication uses Bearer tokens. ASSISTANT_API_TOKEN is the preferred full-client environment variable. Existing COMPANION_API_TOKEN remains supported as a fallback. COMPANION_WIDGET_TOKEN remains supported only for older widget builds with restricted read/done scope.

Free-form capture uses the backend intake service shared with Telegram. Swift does not classify intent. Work tasks, personal tasks, reminders and ideas are persisted by the backend; ideas remain non-actionable until explicitly promoted.

Voice recording itself happens only in the foreground app because WidgetKit cannot access the microphone. The widget microphone deep-links directly into recording mode; after microphone permission has been granted once, the app starts recording immediately after opening. Audio first enters a durable local outbox. On modern iOS the phone attempts on-device SpeechAnalyzer transcription first; the resulting text then enters the normal backend intake/Gemini interpretation path. Server-side audio transcription remains only a fallback. Audio is retained until the text handoff is acknowledged.

## Widget sharing and sideload signing

The current ESign flow re-signs the app and widget with the same code-sign entitlements and therefore the same default Keychain access group. `WidgetSharedSettings` intentionally does not hard-code an App Group as `kSecAttrAccessGroup`: App Groups and Keychain Access Groups are different entitlements.

The app writes the server URL and token to a dedicated Keychain service using that signer-provided default group; the widget reads the same service automatically. This avoids a second widget-configuration flow and remains compatible with the current third-party re-signing profile. Quick Done uses an optimistic local cache so the widget can redraw before waiting for a second Today round-trip.

## System quick capture

On iOS 18+, **Быстрый ввод** is exposed as a Control for Control Center, Lock Screen and Action Button, and as an App Shortcut. It opens Assistant directly into the capture field.

## First run

1. Install the signed IPA.
2. Open Assistant.
3. Enter the HTTPS base URL and full API token once.
4. Tap **Готово**; the app syncs configuration to the widget automatically.
5. Add **Assistant — Сейчас** to the Home Screen.

No separate widget token entry is required for the current ESign build.

## Direction

Do not make iOS a second Telegram UI. Native surfaces should optimize for immediate action; conversational and bulk operations belong in Telegram. Business rules belong in the backend so future clients (web, macOS, Siri/Shortcuts) do not fork behavior.
