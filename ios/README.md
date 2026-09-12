# Assistant Pocket

A deliberately small native iOS companion for Personal Assistant.

## MVP scope

- one free-form capture field;
- native iOS keyboard dictation can be used for voice input;
- Today shows due/overdue work tasks and today's reminders;
- one-tap completion for work tasks;
- no projects screen, no inbox triage screen, no chat clone, no model picker.

Telegram remains the conversational interface. The iOS app is a fast capture and glance surface.

## Backend configuration

Set a new secret on the server:

```bash
COMPANION_API_TOKEN=<long-random-secret>
```

Restart the bot/web service afterwards. The companion routes return `503 companion_not_configured` while the token is absent.

The app needs the public HTTPS base URL of the existing bot web service and the same token. The token is stored in iOS Keychain.

### API

- `GET /api/v1/companion/today`
- `POST /api/v1/companion/capture` with `{ "text": "..." }`
- `POST /api/v1/companion/tasks/{id}/done`

All requests require:

```text
Authorization: Bearer <COMPANION_API_TOKEN>
```

The first MVP captures text literally into the existing `INBOX` project. It intentionally does not duplicate the Telegram LLM intake path yet.

## Build

The repository contains a manual-only GitHub Actions workflow named **iOS IPA**. It never runs on push.

Run it manually when an IPA is needed. The workflow generates the Xcode project with XcodeGen, builds an unsigned device app, packages it as `AssistantPocket.ipa`, and keeps the artifact for three days.

The resulting unsigned IPA is intended for SideStore/AltStore-style signing on the device.

For local macOS development:

```bash
brew install xcodegen
cd ios
xcodegen generate
open AssistantPocket.xcodeproj
```

## Next step after real-world testing

If fast capture proves useful, the next increment should be a Share Extension and a UI-independent intake service shared by Telegram and iOS. Do not call Telegram handlers from the iOS API and do not fork classification rules into Swift.
