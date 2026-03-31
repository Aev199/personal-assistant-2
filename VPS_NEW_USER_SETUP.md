# Инструкция: как поднять ещё одного такого же бота на VPS для нового пользователя

Эта инструкция рассчитана на новичка.

Цель:
- поднять ещё один экземпляр бота на том же VPS
- не сломать уже работающих ботов
- использовать отдельный Telegram-бот, отдельную базу и отдельный сервис

Важно:
- этот бот `single-user`
- он принимает сообщения только от `ADMIN_ID`
- для каждого нового пользователя нужен отдельный экземпляр

Что значит "отдельный экземпляр":
- отдельный `BOT_TOKEN`
- отдельный `ADMIN_ID`
- отдельная база данных
- отдельный Linux user на VPS
- отдельная папка с кодом
- отдельный `systemd` service
- отдельный порт
- отдельный cron на `/tick`

## 1. Что нужно подготовить заранее

Перед началом тебе понадобятся:

1. Telegram-аккаунт нового пользователя
2. Google-аккаунт нового пользователя, если нужен Google Tasks
3. Apple/iCloud-аккаунт нового пользователя, если нужен календарь iCloud
4. Yandex-аккаунт нового пользователя, если нужен WebDAV / Obsidian sync
5. Доступ к VPS по SSH
6. Доступ к репозиторию этого проекта

## 2. Что в итоге получится

После настройки у тебя будет:

1. новый Telegram-бот
2. новая отдельная база на Supabase
3. отдельный процесс на VPS
4. отдельный cron для напоминаний и retry-задач
5. опционально:
   - Gemini LLM
   - Google Tasks
   - iCloud Calendar
   - Yandex Disk WebDAV

## 3. Создай нового Telegram-бота

1. Открой в Telegram `@BotFather`
2. Введи `/newbot`
3. Задай имя бота
4. Задай username бота
5. Сохрани выданный токен

Этот токен будет значением:

```env
BOT_TOKEN=...
```

### Как получить `ADMIN_ID`

`ADMIN_ID` — это numeric Telegram ID того пользователя, которому будет принадлежать этот экземпляр бота.

Самый простой способ:

1. открыть `@userinfobot`
2. нажать `Start`
3. скопировать число

Пример:

```env
ADMIN_ID=123456789
```

## 4. Создай отдельную базу на Supabase Free

Этому боту нужен именно PostgreSQL connection string в `DATABASE_URL`.

Ему не нужны:
- `SUPABASE_URL`
- `anon key`
- `service_role key`

### Шаги

1. Зайди в `https://supabase.com/dashboard`
2. Создай новый проект
3. Выбери имя проекта
4. Выбери регион
5. Задай пароль БД и обязательно сохрани его
6. Дождись, пока проект полностью поднимется

### Как получить строку подключения

1. Открой проект
2. Нажми `Connect`
3. Найди строку подключения Postgres
4. Используй `Session pooler`, а не transaction pooler
5. Если в строке нет SSL-параметра, добавь `?sslmode=require`

Пример:

```env
DATABASE_URL=postgres://postgres.xxxxx:[PASSWORD]@aws-0-eu-central-1.pooler.supabase.com:5432/postgres?sslmode=require
```

### Важно про Supabase Free

1. Для каждого нового пользователя лучше делать отдельный Supabase project
2. Free-проект может приостанавливаться после периода неактивности
3. Если бот внезапно перестал работать спустя время, сначала проверь Supabase Dashboard

## 5. Получи Gemini API key для LLM

Это нужно для:
- свободного текстового ввода
- голосового ввода
- LLM-классификации действий

### Шаги

1. Зайди в `https://aistudio.google.com/`
2. Нажми `Get API key`
3. Создай ключ
4. Сохрани ключ

Используй его так:

```env
GEMINI_API_KEY=...
```

### Важно

1. Можно использовать либо `GEMINI_API_KEY`, либо `GOOGLE_API_KEY`
2. Проще использовать именно `GEMINI_API_KEY`
3. Ключ должен храниться только на сервере, не в Git

## 6. Настрой Google Tasks

Если Google Tasks не нужен, этот раздел можно пропустить.

Нужные переменные:

```env
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
GOOGLE_REFRESH_TOKEN=...
```

### Шаг 1. Создай проект в Google Cloud

1. Зайди в Google Cloud Console
2. Создай новый проект
3. Включи `Google Tasks API`

### Шаг 2. Настрой OAuth consent screen

1. Открой раздел OAuth consent screen
2. Выбери `External`, если это обычный личный Google-аккаунт
3. Заполни минимум обязательных полей
4. Добавь email нового пользователя в `Test users`

### Шаг 3. Создай OAuth client

1. Открой `Credentials`
2. Нажми `Create credentials`
3. Выбери `OAuth client ID`
4. Тип клиента: `Desktop app`
5. Сохрани `client_id` и `client_secret`

### Шаг 4. Получи refresh token

В этом репозитории уже есть готовый helper:

`scripts/get_google_refresh_token.py`

На своём локальном компьютере запусти:

```powershell
cd d:\projects\personal-assistant-2
$env:GOOGLE_CLIENT_ID="твой_client_id"
$env:GOOGLE_CLIENT_SECRET="твой_client_secret"
python .\scripts\get_google_refresh_token.py
```

Дальше:

1. откроется браузер
2. войди под нужным Google-аккаунтом нового пользователя
3. подтверди доступ
4. скрипт выведет строку:

```env
GOOGLE_REFRESH_TOKEN=...
```

Сохрани её.

### Дополнительные env для списков

Можно оставить дефолты:

```env
GTASKS_PERSONAL_LIST=Личное
GTASKS_IDEAS_LIST=Идеи
```

## 7. Настрой iCloud Calendar

Если iCloud Calendar не нужен, этот раздел можно пропустить.

Нужные переменные:

```env
ICLOUD_APPLE_ID=...
ICLOUD_APP_PASSWORD=...
ICLOUD_CALENDAR_URL_WORK=...
ICLOUD_CALENDAR_URL_PERSONAL=...
```

### Шаг 1. Подготовь Apple-аккаунт

1. У Apple-аккаунта должна быть включена двухфакторная аутентификация
2. Зайди на `https://account.apple.com/`
3. Открой `Sign-In and Security`
4. Открой `App-Specific Passwords`
5. Создай app-specific password

Используй:

```env
ICLOUD_APPLE_ID=почта_apple_id
ICLOUD_APP_PASSWORD=app_specific_password
```

### Шаг 2. Получи URL календарей

Самая неудобная часть: боту нужны не просто "рабочий" и "личный" календари, а именно их полные CalDAV collection URL.

То есть вида:

```env
ICLOUD_CALENDAR_URL_WORK=https://caldav.icloud.com/...
ICLOUD_CALENDAR_URL_PERSONAL=https://caldav.icloud.com/...
```

#### Способ 1: Через PowerShell на Windows (рекомендуется)

Открой PowerShell и выполни команды:

```powershell
# Замени на свои данные
$appleId = "your_email@icloud.com"
$appPassword = "xxxx-xxxx-xxxx-xxxx"

# Создаем base64 для авторизации
$credentials = "$appleId`:$appPassword"
$bytes = [System.Text.Encoding]::UTF8.GetBytes($credentials)
$base64 = [Convert]::ToBase64String($bytes)

# Шаг 1: Получаем principal URL
$headers = @{
    "Authorization" = "Basic $base64"
    "Depth" = "0"
    "Content-Type" = "application/xml"
}

$body = @"
<?xml version="1.0" encoding="utf-8"?>
<propfind xmlns="DAV:">
  <prop>
    <current-user-principal/>
  </prop>
</propfind>
"@

try {
    $response1 = Invoke-WebRequest -Uri "https://caldav.icloud.com/" -Method PROPFIND -Headers $headers -Body $body
    Write-Host "✅ Principal response:"
    Write-Host $response1.Content
    Write-Host "`n"
    
    # Парсим principal path из XML (ищем строку вида /12345678/principal/)
    if ($response1.Content -match '<href>([^<]+)</href>') {
        $principalPath = $matches[1]
        Write-Host "📍 Principal path: $principalPath"
        Write-Host "`n"
        
        # Шаг 2: Получаем список календарей
        $body2 = @"
<?xml version="1.0" encoding="utf-8"?>
<propfind xmlns="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <prop>
    <displayname/>
    <c:calendar-description/>
  </prop>
</propfind>
"@

        $headers2 = @{
            "Authorization" = "Basic $base64"
            "Depth" = "1"
            "Content-Type" = "application/xml"
        }
        
        $calendarUrl = "https://caldav.icloud.com$principalPath/calendars/"
        $response2 = Invoke-WebRequest -Uri $calendarUrl -Method PROPFIND -Headers $headers2 -Body $body2
        
        Write-Host "✅ Календари:"
        Write-Host $response2.Content
        Write-Host "`n"
        Write-Host "Ищи в XML теги <displayname> (имя) и <href> (путь к календарю)"
        Write-Host "Полный URL = https://caldav.icloud.com + путь из <href>"
    }
} catch {
    Write-Host "❌ Ошибка: $_"
}
```

Скрипт выведет XML с информацией о календарях. Ищи теги `<displayname>` (имя календаря) и `<href>` (URL календаря).


#### Способ 3: Как понять, какой календарь какой

В XML ответе ищи:
- `<displayname>Work</displayname>` - это имя календаря, которое видно в приложении
- `<href>/12345678/calendars/work/</href>` - это путь к календарю

Полный URL = `https://caldav.icloud.com` + путь из `<href>`

#### Типичные URL календарей iCloud

URL обычно выглядят так:

```
https://caldav.icloud.com/[PRINCIPAL_ID]/calendars/[CALENDAR_ID]/
```

Где:
- `[PRINCIPAL_ID]` - уникальный ID пользователя (обычно длинная строка)
- `[CALENDAR_ID]` - ID конкретного календаря (обычно UUID)

Примеры:

```env
ICLOUD_CALENDAR_URL_WORK=https://caldav.icloud.com/12345678-1234-1234-1234-123456789012/calendars/work/
ICLOUD_CALENDAR_URL_PERSONAL=https://caldav.icloud.com/12345678-1234-1234-1234-123456789012/calendars/home/
```

#### Если не получается найти URL

Если у тебя этих URL пока нет:

1. не блокируй запуск бота
2. оставь эти две переменные пустыми
3. бот всё равно будет работать без iCloud

Можно вернуться к настройке iCloud позже.

## 8. Настрой WebDAV / Yandex Disk / Obsidian

Если синхронизация с Obsidian/Yandex не нужна, этот раздел можно пропустить.

Нужные переменные:

```env
WEBDAV_BASE_URL=https://webdav.yandex.ru
YANDEX_LOGIN=...
YANDEX_PASSWORD=...
VAULT_PATH=/Obsidian/Vault
```

Пример:

```env
WEBDAV_BASE_URL=https://webdav.yandex.ru
YANDEX_LOGIN=user@example.com
YANDEX_PASSWORD=...
VAULT_PATH=/Obsidian/Vault
```

## 9. Подготовь VPS

Ниже пример для пользователя `anna`.

Замени:
- `anna`
- `pa_anna`
- `10002`

на свои значения, если нужно.

### Установи пакеты

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip curl cron
```

Если когда-нибудь включишь backup через `pg_dump`, поставь ещё:

```bash
sudo apt install -y postgresql-client
```

## 10. Создай отдельного Linux user и папку

```bash
sudo adduser --system --group pa_anna
sudo mkdir -p /opt/personal-assistant-anna
sudo chown pa_anna:pa_anna /opt/personal-assistant-anna
```

## 11. Клонируй код и поставь зависимости

```bash
sudo -u pa_anna git clone <URL_ТВОЕГО_РЕПО> /opt/personal-assistant-anna
sudo -u pa_anna bash -lc 'cd /opt/personal-assistant-anna && python3 -m venv .venv && source .venv/bin/activate && pip install -U pip && pip install -r requirements.txt'
```

Если нужны backup backend'ы:

```bash
sudo -u pa_anna bash -lc 'cd /opt/personal-assistant-anna && source .venv/bin/activate && pip install -r requirements-optional.txt'
```

## 12. Выбери отдельный порт

Так как на сервере уже могут быть другие боты, новому инстансу нужен свой порт.

Проверь занятые порты:

```bash
sudo ss -ltnp | grep LISTEN
```

Если не хочешь разбираться, просто возьми:

```env
PORT=10002
```

Если занят, возьми:
- `10003`
- `10004`

## 13. Создай env-файл

Создай файл:

```bash
sudo nano /etc/personal-assistant-anna.env
```

Вставь туда:

```env
BOT_TOKEN=123456:ABCDEF...
ADMIN_ID=123456789
DATABASE_URL=postgres://postgres.xxxxx:[PASSWORD]@aws-0-eu-central-1.pooler.supabase.com:5432/postgres?sslmode=require
INTERNAL_API_KEY=СЛУЧАЙНАЯ_ДЛИННАЯ_СТРОКА

BOT_RUNTIME_MODE=polling-web
HOST=127.0.0.1
PORT=10002
BOT_TIMEZONE=Europe/Moscow
LOG_LEVEL=INFO
LOG_FORMAT=json

GEMINI_API_KEY=
GEMINI_LLM_MODEL=gemini-3.1-flash-lite-preview
GEMINI_TRANSCRIBE_MODEL=gemini-3.1-flash-lite-preview
GEMINI_FALLBACK_1=gemini-3-flash-preview
GEMINI_FALLBACK_2=gemini-2.5-flash
GEMINI_FALLBACK_3=gemini-2.5-flash-lite

GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
GOOGLE_REFRESH_TOKEN=
GTASKS_PERSONAL_LIST=Личное
GTASKS_IDEAS_LIST=Идеи

ICLOUD_APPLE_ID=
ICLOUD_APP_PASSWORD=
ICLOUD_CALENDAR_URL_WORK=
ICLOUD_CALENDAR_URL_PERSONAL=

WEBDAV_BASE_URL=https://webdav.yandex.ru
YANDEX_LOGIN=
YANDEX_PASSWORD=
VAULT_PATH=/Obsidian/Vault
```

### Как сгенерировать `INTERNAL_API_KEY`

```bash
openssl rand -hex 32
```

### Выставь правильные права на env-файл

```bash
sudo chmod 600 /etc/personal-assistant-anna.env
sudo chown pa_anna:pa_anna /etc/personal-assistant-anna.env
```

## 14. Создай systemd service

Создай файл:

```bash
sudo nano /etc/systemd/system/personal-assistant-anna.service
```

Вставь:

```ini
[Unit]
Description=Personal Assistant Bot (anna)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pa_anna
Group=pa_anna
WorkingDirectory=/opt/personal-assistant-anna
EnvironmentFile=/etc/personal-assistant-anna.env
ExecStart=/opt/personal-assistant-anna/.venv/bin/python bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Теперь запусти:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now personal-assistant-anna
sudo systemctl status personal-assistant-anna --no-pager
```

Логи смотреть так:

```bash
journalctl -u personal-assistant-anna -f
```

## 15. Настрой cron для `/tick`

Без cron бот будет отвечать в Telegram, но напоминания и retry-задачи не будут регулярно обрабатываться.

### Создай скрипт

```bash
sudo nano /usr/local/bin/personal-assistant-anna-tick
```

Вставь:

```sh
#!/bin/sh
set -a
. /etc/personal-assistant-anna.env
set +a

curl -fsS -H "X-Internal-Key: $INTERNAL_API_KEY" http://127.0.0.1:10002/tick >/dev/null
```

Сделай исполняемым:

```bash
sudo chmod +x /usr/local/bin/personal-assistant-anna-tick
```

### Создай cron-файл

```bash
sudo nano /etc/cron.d/personal-assistant-anna-tick
```

Вставь:

```cron
*/5 * * * * pa_anna /usr/local/bin/personal-assistant-anna-tick >/dev/null 2>&1
```

Убедись, что cron включён:

```bash
sudo systemctl enable --now cron
sudo systemctl status cron
```

## 16. Проверь, что бот жив

### Проверка HTTP

```bash
curl http://127.0.0.1:10002/health
curl http://127.0.0.1:10002/ping
```

### Проверка tick

```bash
/usr/local/bin/personal-assistant-anna-tick
```

### Проверка Telegram

1. открой нового бота в Telegram
2. нажми `Start`
3. отправь `/start`
4. отправь `/help`

## 17. Что происходит при первом старте

При первом старте бот сам:

1. подключится к базе
2. создаст схему БД
3. поднимет HTTP-сервис
4. запустит polling Telegram

Вручную импортировать `schema.sql` обычно не нужно.

## 18. Что сделать внутри бота после первого запуска

Если это бот не для руководителя, а для обычного исполнителя:

1. открой `⋯ Ещё`
2. нажми `👤 Режим Solo`

Это:

1. уберёт `Команду`
2. заменит её на `⚡ В работе`
3. скроет manager-only функции

## 19. Как потом обновлять этого бота

Когда хочешь обновить код:

```bash
sudo -u pa_anna bash -lc 'cd /opt/personal-assistant-anna && git pull && source .venv/bin/activate && pip install -r requirements.txt'
sudo systemctl restart personal-assistant-anna
```

Если используешь optional зависимости:

```bash
sudo -u pa_anna bash -lc 'cd /opt/personal-assistant-anna && source .venv/bin/activate && pip install -r requirements-optional.txt'
sudo systemctl restart personal-assistant-anna
```

## 20. Как не сломать других ботов на этом же VPS

Главное правило: у каждого инстанса всё своё.

У каждого бота должны быть:

1. свой `BOT_TOKEN`
2. свой `ADMIN_ID`
3. своя база
4. свой Linux user
5. своя папка проекта
6. свой `PORT`
7. свой `systemd` service
8. свой `tick` cron

Если соблюдать это правило, боты работают параллельно и не мешают друг другу.

## 21. Что можно не настраивать сразу

Минимально обязательны только:

```env
BOT_TOKEN=...
ADMIN_ID=...
DATABASE_URL=...
INTERNAL_API_KEY=...
BOT_RUNTIME_MODE=polling-web
HOST=127.0.0.1
PORT=10002
BOT_TIMEZONE=Europe/Moscow
```

Без остальных интеграций бот всё равно будет работать.

Можно отложить на потом:
- Gemini
- Google Tasks
- iCloud
- WebDAV
- backup backend'ы

## 22. Типичные проблемы

### Бот не отвечает в Telegram

Проверь:

```bash
sudo systemctl status personal-assistant-anna --no-pager
journalctl -u personal-assistant-anna -n 100 --no-pager
```

### `/health` отвечает, но напоминания не приходят

Скорее всего проблема в cron или `/tick`.

Проверь:

```bash
systemctl status cron
cat /etc/cron.d/personal-assistant-anna-tick
```

### Google Tasks не работает

Проверь:

1. `GOOGLE_CLIENT_ID`
2. `GOOGLE_CLIENT_SECRET`
3. `GOOGLE_REFRESH_TOKEN`
4. что нужный email добавлен в `Test users`

### Supabase не подключается

Проверь:

1. правильность `DATABASE_URL`
2. наличие `sslmode=require`
3. что Supabase project не paused

### iCloud не работает

Проверь:

1. `ICLOUD_APPLE_ID`
2. `ICLOUD_APP_PASSWORD`
3. правильность `ICLOUD_CALENDAR_URL_WORK`
4. правильность `ICLOUD_CALENDAR_URL_PERSONAL`

## 23. Полезные команды

### Смотреть статус сервиса

```bash
sudo systemctl status personal-assistant-anna --no-pager
```

### Смотреть live-логи

```bash
journalctl -u personal-assistant-anna -f
```

### Перезапустить бота

```bash
sudo systemctl restart personal-assistant-anna
```

### Остановить бота

```bash
sudo systemctl stop personal-assistant-anna
```

### Запустить бота

```bash
sudo systemctl start personal-assistant-anna
```

## 24. Официальные ссылки

- Supabase Postgres connection docs: https://supabase.com/docs/guides/database/connecting-to-postgres
- Google Tasks quickstart: https://developers.google.com/workspace/tasks/quickstart/python
- Google OAuth consent setup: https://developers.google.com/workspace/guides/configure-oauth-consent
- Gemini API quickstart: https://ai.google.dev/gemini-api/docs/get-started/rest
- Gemini setup / API keys: https://ai.google.dev/tutorials/setup
- Apple app-specific passwords: https://support.apple.com/en-afri/102654
- Telegram bots FAQ: https://core.telegram.org/bots/faq
- Yandex Disk auth: https://yandex.com/support/yandex-360/customers/disk/web/en/auth

## 25. Локальные файлы проекта, на которые опирается эта инструкция

- `README.md`
- `bot/config.py`
- `bot/runtime.py`
- `bot/lifecycle.py`
- `bot/db/schema.py`
- `bot/bootstrap.py`
- `scripts/get_google_refresh_token.py`
- `bot/persona.py`
