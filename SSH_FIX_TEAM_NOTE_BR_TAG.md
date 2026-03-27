# Исправление ошибки с тегом <br> в заметке сотрудника

## Проблема
При попытке открыть карточку сотрудника с заметкой, содержащей переносы строк, возникает ошибка:
```
Bad Request: can't parse entities: Unsupported start tag "br" at byte offset 228
```

## Причина
Код заменял переносы строк `\n` на HTML тег `<br>`, но Telegram не поддерживает этот тег в parse_mode="HTML".

## Решение
Убрать `.replace("\n", "<br>")` - переносы строк работают как есть в HTML режиме Telegram.

## Инструкция по применению через SSH

### 1. Подключитесь к VPS
```bash
ssh your_user@your_vps_ip
```

### 2. Перейдите в директорию проекта
```bash
cd /opt/personal-assistant-2
```

### 3. Создайте резервную копию (если еще не создана)
```bash
cp bot/handlers/team.py bot/handlers/team.py.backup.$(date +%Y%m%d_%H%M%S)
```

### 4. Откройте файл для редактирования
```bash
nano bot/handlers/team.py
```

### 5. Найдите строку 260 (около)

Найдите этот код:
```python
        if note:
            lines.append("<b>📝 Заметка</b>")
            lines.append(h(note).replace("\n", "<br>"))
```

### 6. Замените на:
```python
        if note:
            lines.append("<b>📝 Заметка</b>")
            lines.append(h(note))
```

То есть просто удалите `.replace("\n", "<br>")` в конце строки.

### 7. Сохраните файл
- Нажмите `Ctrl+O` для сохранения
- Нажмите `Enter` для подтверждения
- Нажмите `Ctrl+X` для выхода

### 8. Перезапустите бота
```bash
sudo systemctl restart personal-assistant-bot
```

### 9. Проверьте статус
```bash
sudo systemctl status personal-assistant-bot
```

## Проверка исправления

1. Откройте карточку сотрудника, которая не открывалась
2. Карточка должна открыться нормально
3. Заметка с переносами строк должна отображаться корректно

## Дополнительно: Очистка проблемной заметки (если нужно)

Если после исправления карточка все еще не открывается, возможно в заметке есть другие проблемные символы. Можно временно очистить заметку через SQL:

### Через Supabase Dashboard:
1. Откройте SQL Editor
2. Найдите проблемного сотрудника:
```sql
SELECT id, name, note FROM team WHERE note IS NOT NULL;
```
3. Если нужно, очистите заметку:
```sql
UPDATE team SET note = NULL WHERE id = [ID_СОТРУДНИКА];
```

После этого можно будет открыть карточку и добавить заметку заново.

## Откат изменений (если что-то пошло не так)

```bash
cd /opt/personal-assistant-2
cp bot/handlers/team.py.backup.YYYYMMDD_HHMMSS bot/handlers/team.py
sudo systemctl restart personal-assistant-bot
```

Замените `YYYYMMDD_HHMMSS` на дату из имени резервной копии.
