# iOS product contract

The iPhone client is an attention aid, not a smaller project-management system.

## Default surface

The Home Screen widget and **Сегодня** answer only three questions:

1. **На сегодня** — which 3–5 tasks did I choose for today? Tasks due today are on the list automatically and can be removed from it.
2. **Позже сегодня** — which meeting or reminder comes next?
3. **Запомнить** — how do I get a thought out of my head immediately?

«На сегодня» is one list shared by Telegram, the web interface, the app and the widget. It is chosen by tapping, mostly from the 10:00 weekday Telegram message «Что сегодня?». Nothing is dropped overnight: an unfinished task simply returns to the backlog the next day.

The app has three stable top-level destinations: **Сегодня**, **Задачи**, and **Идеи**. Ideas never enter the attention queue until promoted. Projects are context, not top-level navigation.

## ADHD-first rules

- Capture must survive bad network, VPN changes, app suspension and accidental interruption.
- Capture must not depend on the model: without a timely model answer the text becomes a visible Inbox task.
- Voice capture is a first-class capture path, not a separate feature: audio is persisted first, transcribed on-device with SpeechAnalyzer when available, and only the transcript is sent to the normal backend/Gemini intake path. Server audio transcription is the fallback. Recorded audio stays available until the handoff is acknowledged.
- An unfinished capture must survive relaunch.
- There is no single-task focus mode. A due reminder or a meeting in progress (or starting within 15 minutes) is shown above **На сегодня** and never changes the plan itself.
- A reminder takes attention only when it is due. Upcoming reminders remain time anchors in **Позже сегодня** instead of interrupting work early.
- A due reminder has one default escape hatch: **+15 мин**. More complex rescheduling stays off the primary surface.
- Telegram is the reminder delivery channel for now; iOS and the widget are attention/state surfaces. A delivered reminder disappears from native Today on refresh. Native snooze makes the previously delivered Telegram alert inactive instead of leaving two live controls for the same reminder.
- Calendar context stays compact rather than becoming a full calendar screen.
- Acknowledging a calendar event hides it from attention until the event ends; it never deletes or edits the source calendar event. Both the app and widget use a compact explicit dismiss control; the primary surface does not depend on hidden gestures.
- An empty plan offers at most three one-tap suggestions; the full choice is one **Добавить** tap away.
- Proactive messages are weekday-only by default: **Что сегодня?** at 10:00.
- Safe capture actions happen without a confirmation ceremony.
- Ask one concrete follow-up only when required information is genuinely missing.
- Calendar creation is confirmed because it is an external side effect.
- Corrections are available, but editing controls stay off the main screen.
- Do not require inbox-zero, daily planning rituals, scoring, streaks, priority matrices or manual categorization before work can continue.

## Interface rules

- Use the standard iOS bottom tab bar for the three stable destinations: **Сегодня**, **Задачи**, **Идеи**. Do not add more permanent tabs without clear evidence.
- **Сегодня** contains attention and capture only; it does not duplicate global navigation with a **Все задачи** button or whole-screen navigation swipe.
- **Задачи** has exactly two visible scopes: **Рабочие** and **Личные**. The segmented control is explicit; horizontal paging is the fast accelerator between them.
- Horizontal swipes inside **Задачи** belong to scope navigation, so task-row actions must not compete for the same gesture. **Выполнено** and **На сегодня** remain explicit row controls.
- Calendar loading is retried quietly; only an actual calendar failure is shown, so an unknown calendar never looks like a free day.
- Task deadlines always include day context: today/tomorrow or an explicit calendar date.
- No chat clone in the native app.
- No model picker, AI badge, “thinking” copy, generated summaries of obvious UI state, or decorative assistant persona.
- Prefer normal iOS controls and short factual copy.
- Avoid counts and red badges unless the number itself changes what the user should do now.

## Intelligence

Swift does not classify tasks, projects, dates, reminders or intent.

Free-form input is sent to the backend intake service, which uses the same Gemini-first LLM router and domain rules as Telegram. The backend owns business rules and is the source of truth. Deterministic parsing is allowed only for explicit syntax where no model judgment is needed.

## Channel roles

- **Widget:** glance, Quick Done, the **На сегодня** list, one-tap adding of suggested tasks when the list is empty, refresh, text capture and one-tap entry into voice capture.
- **iOS:** three stable tabs — today/capture, work-personal tasks, and saved ideas — plus plan and edit actions.
- **Telegram:** conversational commands, bulk operations and richer interaction while at a PC.
- **Backend:** canonical state, attention ordering, intake, mutations and integrations.

New features should earn a place by reducing cognitive load or interaction cost. If they mainly expose more system state, they probably belong one level deeper or not in the app.
