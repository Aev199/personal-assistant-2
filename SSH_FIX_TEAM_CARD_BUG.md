# Исправление ошибки с карточкой сотрудника

## Проблема
После выхода из редактирования заметки о сотруднике без сохранения, карточка этого сотрудника больше не открывается.

## Причина
Когда пользователь выходит из режима редактирования заметки, но callback не может распарсить данные, функция возвращается без рендеринга UI, оставляя пользователя в подвешенном состоянии.

## Решение
Исправлен обработчик `cb_team_member_details` в файле `bot/handlers/team.py`. Теперь при ошибке парсинга callback данных, состояние FSM очищается и пользователь возвращается к списку команды.

## Инструкция по применению через SSH

### 1. Подключитесь к VPS
```bash
ssh your_user@your_vps_ip
```

### 2. Перейдите в директорию проекта
```bash
cd /opt/personal-assistant-2
```

### 3. Создайте резервную копию файла
```bash
cp bot/handlers/team.py bot/handlers/team.py.backup.$(date +%Y%m%d_%H%M%S)
```

### 4. Откройте файл для редактирования
```bash
nano bot/handlers/team.py
```

### 5. Найдите функцию `cb_team_member_details` (около строки 364)

Найдите этот код:
```python
    await callback.answer()
    await state.clear()

    parsed = _parse_team_member_callback(callback.data)
    if not parsed:
        return

    emp_id, page = parsed
    await ui_render_team_member_card(callback.message, db_pool, emp_id=emp_id, page=page)
```

### 6. Замените на:
```python
    await callback.answer()
    
    parsed = _parse_team_member_callback(callback.data)
    if not parsed:
        await state.clear()
        return await ui_render_team(callback.message, db_pool, force_new=False)

    await state.clear()
    emp_id, page = parsed
    await ui_render_team_member_card(callback.message, db_pool, emp_id=emp_id, page=page)
```

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

### 10. Проверьте логи (если нужно)
```bash
sudo journalctl -u personal-assistant-bot -f
```

## Проверка исправления

1. Откройте карточку любого сотрудника
2. Нажмите "📝 Редактировать заметку" или "📝 Добавить заметку"
3. Нажмите "✖️ Отмена" или "⬅ Назад"
4. Попробуйте снова открыть карточку этого сотрудника из списка команды
5. Карточка должна открыться без проблем

## Откат изменений (если что-то пошло не так)

```bash
cd /opt/personal-assistant-2
cp bot/handlers/team.py.backup.YYYYMMDD_HHMMSS bot/handlers/team.py
sudo systemctl restart personal-assistant-bot
```

Замените `YYYYMMDD_HHMMSS` на дату из имени резервной копии.
