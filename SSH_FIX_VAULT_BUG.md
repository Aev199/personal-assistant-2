# Исправление ошибки "name 'vault' is not defined"

## Проблема

При создании нового проекта возникает ошибка:
```
❌ Ошибка загрузки. Для фикса: name 'vault' is not defined
```

## Причина

В файле `bot/handlers/projects.py` строка 178 использует `vault` вместо `deps.vault`.

## Исправление через SSH

### Вариант 1: Автоматическое исправление (одна команда)

```bash
ssh user@your-vps
cd /opt/personal-assistant-2

# Создайте backup и примените исправление
cp bot/handlers/projects.py bot/handlers/projects.py.backup.$(date +%Y%m%d_%H%M%S) && \
sed -i 's/background_project_sync(\s*$/background_project_sync(/; /background_project_sync(/,/error_logger=deps.db_log_error/{s/vault,/deps.vault,/}' bot/handlers/projects.py && \
echo "✓ Исправление применено" && \
sudo systemctl restart personal-assistant-bot && \
echo "✓ Бот перезапущен"
```

### Вариант 2: Ручное исправление

```bash
# 1. Создайте backup
cp bot/handlers/projects.py bot/handlers/projects.py.backup.$(date +%Y%m%d_%H%M%S)

# 2. Откройте файл
nano bot/handlers/projects.py

# 3. Найдите строку 178 (Ctrl+_ затем введите 178)
# Или найдите через Ctrl+W: background_project_sync

# 4. Найдите эту секцию:
        fire_and_forget(
            background_project_sync(
                int(project_id),
                db_pool,
                vault,  # ← ЗДЕСЬ ОШИБКА
                error_logger=deps.db_log_error,
            ),
            label=f"sync:proj:{int(project_id)}",
        )

# 5. Измените vault на deps.vault:
        fire_and_forget(
            background_project_sync(
                int(project_id),
                db_pool,
                deps.vault,  # ← ИСПРАВЛЕНО
                error_logger=deps.db_log_error,
            ),
            label=f"sync:proj:{int(project_id)}",
        )

# 6. Сохраните (Ctrl+O, Enter, Ctrl+X)
```

### Вариант 3: Через sed (точечное исправление)

```bash
# Создайте backup
cp bot/handlers/projects.py bot/handlers/projects.py.backup.$(date +%Y%m%d_%H%M%S)

# Примените исправление
sed -i '178s/vault,/deps.vault,/' bot/handlers/projects.py

# Проверьте что изменилось
sed -n '175,185p' bot/handlers/projects.py
```

### Шаг 3: Перезапустите бота

```bash
sudo systemctl restart personal-assistant-bot
```

### Шаг 4: Проверьте

```bash
# Логи
journalctl -u personal-assistant-bot -f
```

Теперь создание проекта должно работать!

## Проверка исправления

```bash
# Проверьте строку 178
sed -n '178p' bot/handlers/projects.py
```

Должно быть:
```python
                deps.vault,
```

А не:
```python
                vault,
```

## Откат (если что-то пошло не так)

```bash
cp bot/handlers/projects.py.backup.* bot/handlers/projects.py
sudo systemctl restart personal-assistant-bot
```

## Тест

После исправления попробуйте создать проект в боте:
```
/add
→ Выберите "Проект"
→ Введите: K-99 Тестовый проект
```

Должно показать:
```
✅ Проект K-99 создан и связан с файлом Тестовый проект.md
```

---

**Причина бага:** Переменная `vault` не определена в локальной области функции, нужно использовать `deps.vault`.
