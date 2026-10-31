# Personal Assistant

Личный помощник для задач, напоминаний и встреч одного человека.
Цель — разгрузить память: быстро записать мысль, утром выбрать дела на день,
не потерять ни одной записи.

## Как устроено

Один Python-сервис (`bot.py`) и одна база PostgreSQL. Все клиенты работают
с общими данными и одним способом записи.

| Клиент | Роль |
|---|---|
| **Telegram** | Записать текстом или голосом. «Сегодня», «Все дела», карточка задачи. Напоминания, а по будням в 10:00 — «Что сегодня?» |
| **iPhone** (`ios/`) | Три вкладки «Сегодня», «Задачи», «Идеи». Виджет «На сегодня», голосовая запись с кнопки действия |
| **Веб** (`/app/`) | Рабочий компьютер: списки, разбор «Входящих», запись |

**Запись.** Текст разбирает Gemini: задача, личное дело, напоминание, идея или
встреча. Если Gemini недоступен или не ответил за 10 секунд, текст сохраняется
во «Входящие». Встречу в календарь iCloud нужно подтвердить одной кнопкой.
Неизвестный проект — запись попадает во «Входящие», неизвестный человек —
исполнитель остаётся пустым. Только что созданную запись можно отменить в
течение 15 минут.
Напоминание создаётся, только если вы явно просите напомнить («напомни…»):
«позвонить подрядчику завтра» — это задача со сроком. Все напоминания видны
в списке (Telegram «⋯ → Напоминания», колокольчик в «Задачах» на iPhone, раздел
в вебе): любое можно удалить или сделать задачей.

**На сегодня.** Это короткий список дел на день, общий для всех клиентов. В нём
дела, выбранные на сегодня, и дела со сроком на сегодня (их можно убрать из списка).
Ночью ничего не пропадает: невыполненное просто остаётся в общем списке.

**Календарь** iCloud читается для встреч на сегодня. Создание встречи требует
подтверждения.

## Код

```
bot/
  tg/             Telegram: запись, экраны, карточка задачи
  http/           API для iPhone и веба, служебные адреса (/tick, /health)
  web/            веб-интерфейс (один HTML, CSS и JS)
  services/       общая логика: запись (native_intake + intake_rules),
                  «На сегодня» (day_plan), утренние сообщения, напоминания (tick)
  adapters/       Gemini, DeepSeek (запасной), iCloud CalDAV
  db/             схема и служебные таблицы
ios/              Swift-приложение и виджет (XcodeGen)
tests/            тесты сервера, контракт iPhone-клиента, проверка веба в браузере
```

Схема базы создаётся и дополняется сама при запуске; изменения только добавочные.

## Сервер (VPS)

- Каталог `/opt/personal-assistant-2`, владелец `personalassistant`.
- Служба `personal-assistant-bot.service`, слушает `127.0.0.1:10001`.
- Настройки в `/etc/personal-assistant-bot.env`. Секреты в репозиторий не попадают.
- Nginx проксирует HTTPS на сервис.
- Cron раз в минуту вызывает `/tick` (`/etc/cron.d/personal-assistant-bot-tick`):
  напоминания и утренние сообщения работают только с ним.

Обновление (под root):

```bash
cd /opt/personal-assistant-2
sudo -u personalassistant git status --short      # должно быть пусто
sudo -u personalassistant git rev-parse --short HEAD   # запишите для отката
sudo -u personalassistant git fetch origin
sudo -u personalassistant git switch main 2>/dev/null || sudo -u personalassistant git switch --track origin/main
sudo -u personalassistant git pull --ff-only
systemctl restart personal-assistant-bot.service
systemctl status personal-assistant-bot.service --no-pager | head -5
curl -s http://127.0.0.1:10001/health
```

Откат: `sudo -u personalassistant git switch --detach ЗАПИСАННАЯ-ВЕРСИЯ` и тот же перезапуск.

Второй экземпляр для другого человека: [docs/second-instance.md](docs/second-instance.md).

## Настройки

Обязательные:

| Переменная | Что это |
|---|---|
| `BOT_TOKEN` | токен Telegram-бота |
| `ADMIN_ID` | ваш Telegram id; бот отвечает только вам |
| `DATABASE_URL` | PostgreSQL |
| `INTERNAL_API_KEY` | ключ для `/tick` (заголовок `X-Internal-Key`) |
| `ASSISTANT_API_TOKEN` | ключ приложения: iPhone, виджет, вход в веб (старое имя `COMPANION_API_TOKEN` тоже работает) |
| `ASSISTANT_PUBLIC_ORIGIN` | адрес сайта для веб-входа, например `https://example.org` |
| `GEMINI_API_KEY` | разбор записи и голоса |
| `BOT_TIMEZONE` | часовой пояс, по умолчанию `Europe/Moscow` |

Дополнительные:

| Переменная | Что это |
|---|---|
| `WEBHOOK_URL` | если задан, Telegram работает через webhook, иначе через polling |
| `ICLOUD_APPLE_ID`, `ICLOUD_APP_PASSWORD`, `ICLOUD_CALENDAR_URL_WORK`, `ICLOUD_CALENDAR_URL_PERSONAL`, `ICLOUD_CALENDAR_URL_BITRIX` | календари iCloud |
| `DEEPSEEK_API_KEY` | запасная модель, если Gemini недоступен |
| `ASSISTANT_MORNING_BRIEF_HOUR` / `ASSISTANT_EVENING_NUDGE_HOUR` | 10 и 17 по умолчанию |
| `ASSISTANT_EVENING_NUDGE=1` | вечернее напоминание про «Входящие» (по умолчанию выключено) |
| `ASSISTANT_WEEKEND_BRIEF=1` | сообщения и в выходные (по умолчанию нет) |
| `NATIVE_INTAKE_CLASSIFY_TIMEOUT_SEC` | сколько ждать Gemini перед записью во «Входящие», 10 |

Переменные Render, Obsidian/Яндекс.Диска, облачных копий и Google Tasks больше
не читаются, их можно удалить из файла настроек.

## Проверки

```bash
pip install -r requirements.txt pytest
python -m pytest -q
TEST_DATABASE_URL=postgresql://…/test python -m pytest -q   # плюс проверки на настоящей базе
node tests/web-browser.cjs                                  # веб в браузере (нужен Playwright)
```

На GitHub проверка `ci` запускает всё это при каждом изменении `main` и в
pull request. IPA собирается вручную: Actions → iOS IPA → Run workflow.
