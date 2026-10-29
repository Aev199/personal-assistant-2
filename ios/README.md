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

1. **На сегодня** — the shared list of today's tasks (planned for today, or due today and not removed). A meeting now or a due reminder is shown above it.
2. **Позже сегодня** — the next meeting and reminder.
3. **Запомнить** — fast capture.

The bottom tabs are **Сегодня**, **Задачи**, **Идеи**. **Задачи** loads the complete active backlog (up to 500 tasks) and every row has explicit **Выполнено** and **На сегодня** controls.

The standard bottom tab bar contains **Сегодня**, **Задачи**, and **Идеи**. Tasks provides the complete active backlog with work/personal scopes and local search. Ideas stay outside the attention queue until explicitly promoted to a task.

The Home Screen widget uses StaticConfiguration; configurable AppIntentConfiguration is intentionally avoided because it fails after the sideload re-signing flow.

## Backend API

Canonical native-client routes:

- GET /api/v1/today
- GET /api/v1/tasks?limit=500
- GET /api/v1/ideas
- POST /api/v1/ideas/{id}/promote
- POST /api/v1/ideas/{id}/archive
- POST /api/v1/intake with a JSON text field (the same capture core as Telegram and the web)
- GET /api/v1/intake/pending, POST /api/v1/intake/{id}/confirm and /cancel — calendar events wait for one tap
- POST /api/v1/intake/audio with multipart AAC/M4A voice data
- POST /api/v1/tasks/{id}/done
- POST /api/v1/tasks/{id}/plan and /unplan — add to or remove from «На сегодня»; tasks carry `today_plan`
- PATCH /api/v1/tasks/{id} with `expected_updated_at`; a change made meanwhile from the web or Telegram returns 409 `task_conflict` instead of being overwritten

These are the only routes; the old /api/v1/companion aliases, the focus endpoints and the separate widget token were removed.

Authentication uses one Bearer token, ASSISTANT_API_TOKEN (the older name COMPANION_API_TOKEN is still read). The app and the widget share it.

Free-form capture uses the backend intake service shared with Telegram. Swift does not classify intent. Work tasks, personal tasks, reminders and ideas are persisted by the backend; ideas remain non-actionable until explicitly promoted.

If the model is unavailable or does not answer within `NATIVE_INTAKE_CLASSIFY_TIMEOUT_SEC` (default 10 s), the backend saves the text as an Inbox task with the client capture id as the shared retry key, and answers `inbox_fallback: true`. The phone shows «Записано во Входящие»; the record is immediately visible in the app, the widget, the web interface and Telegram.

Voice recording itself happens only in the foreground app because WidgetKit cannot access the microphone. The widget microphone deep-links directly into recording mode; after microphone permission has been granted once, the app starts recording immediately after opening. Audio first enters a durable local outbox. On modern iOS the phone attempts on-device SpeechAnalyzer transcription first; the resulting text then enters the normal backend intake/Gemini interpretation path. Server-side audio transcription remains only a fallback. Audio is retained until the text handoff is acknowledged.

## Widget sharing and sideload signing

The IPA is unsigned and is installed through SideStore. The sideload flow re-signs the app and widget with the same code-sign entitlements and therefore the same default Keychain access group. `WidgetSharedSettings` intentionally does not hard-code an App Group as `kSecAttrAccessGroup`: App Groups and Keychain Access Groups are different entitlements.

The app writes the server URL and token to a dedicated Keychain service using that signer-provided default group; the widget reads the same service automatically. This avoids a second widget-configuration flow and remains compatible with the current third-party re-signing profile. Quick Done uses an optimistic local cache so the widget can redraw before waiting for a second Today round-trip.

## System quick capture

On iOS 18+, **Быстрый ввод** is exposed as a Control for Control Center, Lock Screen and Action Button, and as an App Shortcut. It opens Assistant directly into the capture field. **Голосовая запись** is a second Control and App Shortcut that opens Assistant and starts recording immediately.

## First run

1. Install the IPA through SideStore.
2. Open Assistant.
3. Enter the HTTPS base URL and full API token once.
4. Tap **Готово**; the app syncs configuration to the widget automatically.
5. Add **Assistant — На сегодня** to the Home Screen.
6. Optional: assign **Голосовая запись** to the Action Button or the Lock Screen; one press starts recording.

No separate widget token entry is required.

## Direction

Do not make iOS a second Telegram UI. Native surfaces should optimize for immediate action; conversational and bulk operations belong in Telegram. Business rules belong in the backend so future clients (web, macOS, Siri/Shortcuts) do not fork behavior.
