# Assistant Pocket

A deliberately small native iOS companion for Personal Assistant.

## MVP scope

- one free-form capture field in the app;
- native iOS keyboard dictation can be used for voice input;
- a medium/large Home Screen widget shows Today;
- interactive Quick Done works directly from the widget on iOS 17+;
- the widget `+` opens the app directly into focused capture;
- no projects screen, no inbox triage screen, no chat clone, no model picker.

Telegram remains the conversational interface. The iOS app is mostly configuration and capture; the widget is intended to be the daily surface.

## Backend configuration

Set two secrets on the server:

```bash
COMPANION_API_TOKEN=<long-random-secret>
COMPANION_WIDGET_TOKEN=<different-long-random-secret>
```

`COMPANION_API_TOKEN` is the full companion token used by the app. `COMPANION_WIDGET_TOKEN` is intentionally restricted: it can read Today and mark tasks done, but it cannot create captures. The regular app token is also accepted by read/done endpoints for backward compatibility, but the separate widget token is recommended.

Restart the bot/web service after changing environment variables.

### API

- `GET /api/v1/companion/today`
- `POST /api/v1/companion/capture` with `{ "text": "..." }`
- `POST /api/v1/companion/tasks/{id}/done`

Requests use Bearer authentication.

The first MVP captures text literally into the existing `INBOX` project. It intentionally does not duplicate the Telegram LLM intake path yet.

## Why widget settings are separate

The sideload build does not use App Groups. SideStore currently has a known issue where App Group storage used by widget extensions is not available reliably after sideload signing. Avoiding App Groups keeps the widget usable with SideStore, at the cost of entering the server URL and widget token once in the widget configuration.

## Build

The repository contains a manual-only GitHub Actions workflow named **iOS IPA**. It never runs on push.

Run it manually when an IPA is needed. The workflow generates the Xcode project with XcodeGen, builds an unsigned device app together with its widget extension, packages it as `AssistantPocket.ipa`, and keeps the artifact for three days.

The resulting unsigned IPA is intended for SideStore/AltStore-style signing on the device.

For local macOS development:

```bash
brew install xcodegen
cd ios
xcodegen generate
open AssistantPocket.xcodeproj
```

## First run on iPhone

1. Install the generated `AssistantPocket.ipa` with SideStore.
2. Open the app; the connection sheet appears automatically.
3. Enter the public HTTPS base URL of the Personal Assistant web service without a trailing slash.
4. Enter `COMPANION_API_TOKEN` and tap **Готово**.
5. Add the **Assistant — Сегодня** widget to the Home Screen.
6. Long-press the widget → **Edit Widget** and enter the same HTTPS base URL plus `COMPANION_WIDGET_TOKEN`.
7. The circle next to a task completes it without opening the app. The `+` button opens the app with the capture field focused.

The widget refreshes on the system timeline and also requests a refresh immediately after Quick Done.

## Next step after real-world testing

If this interaction proves useful, the next increment should be a Share Extension and then a UI-independent intake service shared by Telegram and iOS. Do not call Telegram handlers from the iOS API and do not fork classification rules into Swift.
