# Unified Assistant Web Interface Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Proposed execution: native, in this session; requires user review before implementation.

**Goal:** Подключить согласованный адаптивный веб-интерфейс к существующим задачам, идеям и расписанию помощника.

**Architecture:** Тот же aiohttp-процесс раздаёт `/app/` и обслуживает `/api/v1`. PWA, Telegram и iOS используют одну Postgres-базу; существующие Bearer-клиенты совместимы. Статический интерфейс вызывает API через защищённую браузерную сессию.

**Tech Stack:** Python, aiohttp, asyncpg, Postgres, HTML/CSS/JavaScript; pytest; Playwright для проверки браузера, если доступен в окружении. Новые зависимости на VPS не требуются.

**Spec:** `docs/superpowers/specs/2026-10-17-unified-assistant-pwa-design.md`

## Global Constraints

- База реализации: `feat/ios-primary-client` на `bcc60dbb375fdda58538020c2c624bf9aa44efa2`, не устаревшая `main`.
- Одна база, серверные правила, старые маршруты iOS сохраняются.
- «Сегодня», «Задачи», «Расписание», «Входящие», «Идеи», действие «Сейчас»; без мотивирующих названий.
- Нижние мобильные вкладки: Главная / Задачи / Идеи; работа/личное внутри задач.
- Запись не требует LLM; подтверждение сохранения появляется после фиксации в БД.
- Токены не сохраняются в localStorage, URL, HTML или service worker.
- Изменения схемы только добавочные. Уведомления и Swift не переключаются в первом этапе.
- Manifest, иконки, service worker и автономное редактирование не входят в первый этап. Не называть веб-интерфейс уже установленной PWA.
- Согласованный макет задаёт внешний вид; демонстрационные записи не добавляются на сервер.

## Review Focus

- Два одновременных повтора одного сохранения дают одну задачу, даже после потери ответа.
- Второе устройство изменило задачу: старое редактирование получает конфликт вместо перезаписи.
- Более 500 задач и фильтр «личное»: подходящие записи доступны за пределами первой страницы.
- Чужой Origin, истёкшая сессия или widget-токен не дают браузерных прав на изменение.
- iCloud недоступен или сеть оборвалась: задачи доступны, черновик не исчезает и расписание не выглядит пустым.

---

## File Structure

- `bot/http/web_auth.py`: браузерные сессии, вход/выход, Origin/CSRF, ограничение входа.
- `bot/http/web_app.py`: раздача разрешённых статических файлов и регистрация web-auth.
- `bot/web/index.html`, `app.css`, `app.js`: интерфейс, стили, запросы и экранные состояния.
- `bot/services/web_sessions.py`: хранение хешированных сессий, expiry и отзыв при ротации токена.
- `bot/services/task_browser.py`: полные списки, сериализация версии, атомарное редактирование и triage.
- `bot/services/capture.py`: сохранение без LLM с необязательным ключом повторного запроса.
- `bot/db/schema.py`, `schema.sql`: одинаковые добавочные таблицы сессий и ключей повторов.
- `bot/http/companion.py`: совместимая авторизация и вызов новых общих операций.
- `bot/runtime.py`: подключение web routes в webhook и polling-web.
- `tests/test_web_auth.py`, `test_capture_idempotency.py`, `test_task_browser.py`, `test_web_app.py`: проверки поведения соответствующих модулей.
- `tests/test_web_postgres.py`: транзакционные проверки с отдельной тестовой БД, заданной `TEST_DATABASE_URL`; не подключаться к рабочей БД.
- `docs/web-interface.md`: установка, вход, проверка и откат на VPS.

### Task 1: Browser session and static application entry

**Files:** web_auth.py, web_app.py, web_sessions.py, schema.py, schema.sql, runtime.py; tests/test_web_auth.py, tests/test_web_app.py.

**Interfaces:** `attach_web_routes(app: web.Application, ctx) -> None`; `browser_identity(request: web.Request) -> dict | None`. Middleware проверяет сессию на `/api/v1/` и записывает `request['assistant_browser_identity']`; companion._authorized дополнительно принимает эту проверенную сервером identity. Bearer-путь не меняется.

- [ ] Написать тесты HTTP: вход верным app-токеном → 201 и cookie; widget-токен → 401; защищённое чтение без входа → 401; выход → сессия больше не действует; `/app/` → 200. Применять aiohttp TestServer/TestClient через asyncio.run, как существующие тесты без нового pytest-плагина.
- [ ] Запустить `pytest -q tests/test_web_auth.py tests/test_web_app.py`; подтвердить отказ до реализации.
- [ ] Добавить `web_sessions(session_hash TEXT PRIMARY KEY, token_fingerprint TEXT NOT NULL, csrf_token TEXT NOT NULL, expires_at TIMESTAMPTZ NOT NULL)` в обе схемы. Хранить SHA-256 случайных секретов; сессия 30 дней, cookie `assistant_session` с Path=/, HttpOnly, Secure, SameSite=Strict. Истечение проверять в БД на каждом запросе; ротация app-токена делает fingerprint непригодным. Удалять просроченные строки при создании сессии.
- [ ] Реализовать `POST /api/v1/session` с JSON `{token}`, `GET /api/v1/session` с `{ok, csrf_token}`, `DELETE /api/v1/session`. Использовать постоянный CSRF-секрет сессии из 32 случайных байтов; хранить csrf_token на сервере и возвращать клиенту при входе и GET session. GET не меняет секрет, чтобы вкладки не инвалидировали друг друга. Сравнение constant-time, ответы сессии Cache-Control:no-store.
- [ ] Требовать `ASSISTANT_PUBLIC_ORIGIN` с точным HTTPS origin. Проверять Origin входа и всех cookie-изменений; для изменений также X-CSRF-Token. Header Origin не вычислять из произвольного Host/X-Forwarded-Host. На Bearer-клиентов CSRF не распространяется. Вход ограничить пятью неуспешными попытками в минуту на IP и 30 глобально; доверие proxy headers не включать автоматически.
- [ ] Добавить тесты expired/revoked session, cross-origin login/mutation, missing CSRF, widget scope и параллельных вкладок. Static routes обслуживают только перечисленные файлы, без directory traversal, API и shell не кэшируются. Регистрация проверяется для обоих runtime modes.
- [ ] Запустить новые тесты и `pytest -q tests/test_companion_http.py`; затем закоммитить Task 1.

### Task 2: Durable capture without duplicate tasks

**Files:** services/capture.py, companion.py, schema.py, schema.sql; tests/test_capture_idempotency.py, tests/test_web_postgres.py.

**Interfaces:** `save_capture(conn, *, chat_id: int, text: str, client_request_id: str | None, source: str) -> dict`; caller owns transaction. Возвращает прежнюю структуру `{ok, task:{id,title,project}}`. Повтор с иным текстом вызывает `CaptureConflict`, HTTP 409.

- [ ] Тесты: валидный текст без LLM → INBOX; тот же ключ/текст → тот же task.id; тот же ключ/другой текст → конфликт; не-object JSON и пустой текст → 400; более 2000 символов → 413; без ключа сохраняется прежнее поведение iOS.
- [ ] Запустить `pytest -q tests/test_capture_idempotency.py`, подтвердить отказ.
- [ ] Добавить `capture_requests(chat_id BIGINT, request_id TEXT, text_hash TEXT NOT NULL, result_json JSONB NOT NULL, PRIMARY KEY(chat_id, request_id))`. Ключ — UUID; сервер ограничивает длину и формат. Сериализовать конкурирующие ключи транзакционной advisory lock, сохранять задачу и результат в одной транзакции. Не использовать in-memory dedupe. Не удалять ключи в первом этапе.
- [ ] Обновить handle_capture: object validation, необязательный client_request_id, source='web' для сессии и 'ios' для прежнего Bearer. Существующий формат 201 сохраняется и при повторе.
- [ ] В отдельной Postgres-БД проверить два конкурентных запроса и повтор после отката транзакции: одна запись при успехе, ни задачи ни ключа после отката.
- [ ] Запустить тесты Task 2 и companion; закоммитить.

### Task 3: Complete lists, edit conflicts and Inbox-to-idea

**Files:** services/task_browser.py, companion.py; tests/test_task_browser.py, tests/test_web_postgres.py.

**Interfaces:** `list_tasks(conn, *, scope: str, inbox: bool, limit: int, cursor: int | None) -> dict`; `update_task(conn, *, task_id: int, changes: dict, expected_updated_at: str | None) -> dict`; `move_inbox_to_idea(conn, *, chat_id: int, task_id: int) -> int`. Все мутации вызываются внутри транзакции.

- [ ] Тесты: scope work/personal/all; INBOX; 501+ задач; две страницы без повторов; title/date/project edit; устаревший updated_at → 409; перевод INBOX в идею и отказ вставки не теряет исходник.
- [ ] Запустить `pytest -q tests/test_task_browser.py`, подтвердить отказ.
- [ ] GET tasks: если переданы `scope`, `cursor` или `inbox`, включить browse-режим с сортировкой id ASC и keyset `id > cursor`, limit 1..200, фильтрами до LIMIT и limit+1 для next_cursor. Сохранить существующие поля tasks, добавить next_cursor и updated_at. Запрос без новых параметров оставляет нативную сортировку; новый UI всегда использует browse-режим. «Сегодня» остаётся подборкой.
- [ ] PATCH tasks: получать updated_at под FOR UPDATE, проверять expected_updated_at при наличии; браузер обязан прислать версию, старые Bearer-клиенты могут её не передавать. Один UPDATE на все изменённые поля и новый updated_at в ответе. Даты вводятся и отображаются в серверной timezone; отсутствие даты передаётся null.
- [ ] POST `/api/v1/tasks/{task_id}/idea`: полные права, блокировка строки, только активная INBOX-задача kind=task; INSERT ideas и status='done' исходной задачи в одной транзакции с событием task_converted_to_idea. Повтор не создаёт вторую идею; конфликт → 409. Остальные задачи не затрагиваются.
- [ ] Запустить unit и Postgres-проверки Task 3, tests/test_companion_http.py и tests/test_ideas.py; закоммитить.

### Task 4: Connected desktop and mobile interface

**Files:** bot/web/index.html, app.css, app.js; tests/test_web_app.py.

**Interfaces:** `api(path, {method, body}={}) -> Promise<object>` использует same-origin cookie и CSRF в памяти. Screens: today, tasks, inbox, ideas. State хранит scope, cursor, task list и pending capture; отдельной клиентской базы нет.

- [ ] Проверить HTTP-тестом: `/app/` и ресурсы доступны, Content-Type корректен, protected API не отдаёт данные без входа. Запустить тест до раздачи полноценного интерфейса.
- [ ] Перенести визуальный язык согласованного макета: строки задач, боковая панель, расписание справа, карточка деталей. На узком экране — три нижние вкладки; работа/личное переключаются кнопками и горизонтальным свайпом только в области списка, с порогом направления и без перехвата вертикальной прокрутки.
- [ ] Реализовать login/logout, Today, paginated Tasks, Inbox, Ideas и детали. Использовать существующие today/projects/ideas/focus/unfocus/done/promote/archive API и расширения Tasks 2–3. Назначение «Сейчас» и быстрое завершение доступны в строке/деталях. Не добавлять неработающие навигационные пункты.
- [ ] Черновик capture и UUID сохранять в sessionStorage до подтверждения; секреты там отсутствуют. Повтор после сети сохраняет ключ. Текст после отправки не стирать, если пользователь успел набрать другой. 401 открывает вход с сохранённым черновиком; 409 редактирования предлагает обновить карточку.
- [ ] Обновлять активную вкладку каждые 30 секунд и на visibilitychange; скрытая вкладка не опрашивает. Отменять устаревшие fetch/игнорировать их результаты при смене scope. Не заменять несохранённую форму фоновым ответом. Календарь error/unavailable отображать отдельно от пустого списка.
- [ ] Проверить браузером ширины 1440 и 390 px: вход, capture success/failure/retry, пагинацию, edit conflict, cross-device refresh, работу клавиатурой и закрытие dialog Escape. Тестовый API явно отделён от production и не пишет в рабочую БД. Скриншоты фиксируют реальный интерфейс с обозначенными тестовыми данными.
- [ ] Запустить tests/test_web_app.py и проверки JS `node --check bot/web/app.js`; закоммитить.

### Task 5: Regression verification and VPS handoff

**Files:** docs/web-interface.md, README.md; при необходимости tests workflow, без автоматического запуска iOS-сборки.

**Interfaces:** документация задаёт ASSISTANT_PUBLIC_ORIGIN и уже существующий app-токен; новый процесс или внешний frontend deployment не требуется.

- [ ] Выполнить `pytest -q` и записать точные результаты, отдельно указать пропущенные интеграционные проверки. Выполнить Postgres-набор с временной изолированной БД; проверить миграции с чистой схемы и повторный ensure_schema.
- [ ] Проверить git diff на удаление старых API и изменения доставки уведомлений: таких изменений быть не должно. Проверить Dockerfile включает web assets через существующий COPY.
- [ ] Написать короткую инструкцию: резервная копия БД, переход на выбранный коммит, переменная origin, проверка HTTPS reverse proxy для /app и /api/v1, перезапуск существующего systemd service и /health. Название службы определить на VPS, не угадывать. Откат — предыдущий commit и перезапуск; добавочные таблицы можно оставить.
- [ ] Повторить smoke без Gemini и без iCloud; убедиться, что обычная запись работает, а календарь сообщает о недоступности.
- [ ] Предоставить пользователю скриншоты и инструкцию, перечислить проверки, которые требуют реального iPhone/VPS. Сохранить ветку в GitHub, не разворачивать на недоступном сервере и не утверждать, что установочная PWA уже готова.

## Follow-up boundary

Следующие самостоятельные этапы после приёмки веб-интерфейса: установочная PWA;
затем сокращение Swift и отдельная реализация единственного канала уведомлений
на iOS с проверкой на устройстве. Этот план их не выполняет.
