# iOS product contract

The iPhone client is an attention aid, not a smaller project-management system.

## Default surface

The Home Screen widget and **Сегодня** answer only three questions:

1. **Сейчас** — what deserves attention now?
2. **Дальше** — what is next, in a deliberately short list?
3. **Запомнить** — how do I get a thought out of my head immediately?

The full backlog is one level deeper. Ideas are deeper still and never enter the attention queue until promoted. Projects are context, not navigation.

## ADHD-first rules

- Capture must survive bad network, VPN changes, app suspension and accidental interruption.
- An unfinished capture must survive relaunch.
- There is one explicit current focus. Choosing a new focus must not require reorganizing the backlog.
- A reminder that is due or within 15 minutes may temporarily take **Сейчас**; it must not be buried behind ordinary tasks.
- **Дальше** stays deliberately short: at most four rows on the main screen.
- Safe capture actions happen without a confirmation ceremony.
- Ask one concrete follow-up only when required information is genuinely missing.
- Calendar creation is confirmed because it is an external side effect.
- Corrections are available, but editing controls stay off the main screen.
- Do not require inbox-zero, daily planning rituals, scoring, streaks, priority matrices or manual categorization before work can continue.

## Interface rules

- No permanent tab bar unless real usage proves one is necessary.
- No chat clone in the native app.
- No model picker, AI badge, “thinking” copy, generated summaries of obvious UI state, or decorative assistant persona.
- Prefer normal iOS controls and short factual copy.
- Avoid counts and red badges unless the number itself changes what the user should do now.

## Intelligence

Swift does not classify tasks, projects, dates, reminders or intent.

Free-form input is sent to the backend intake service, which uses the same Gemini-first LLM router and domain rules as Telegram. The backend owns business rules and is the source of truth. Deterministic parsing is allowed only for explicit syntax where no model judgment is needed.

## Channel roles

- **Widget:** glance, Quick Done, refresh, quick capture.
- **iOS:** today, capture, choose focus, correct a task, browse/search active work, review saved ideas secondarily.
- **Telegram:** conversational commands, bulk operations and richer interaction while at a PC.
- **Backend:** canonical state, attention ordering, intake, mutations and integrations.

New features should earn a place by reducing cognitive load or interaction cost. If they mainly expose more system state, they probably belong one level deeper or not in the app.
