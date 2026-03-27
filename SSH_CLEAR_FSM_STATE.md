# Очистка застрявшего FSM состояния (Supabase)

## Проблема
Карточка конкретного сотрудника не открывается после выхода из редактирования заметки. FSM состояние могло застрять в базе данных.

## Решение для Supabase

### Способ 1: Через Supabase Dashboard (самый простой)

1. Откройте [Supabase Dashboard](https://supabase.com/dashboard)
2. Выберите ваш проект
3. Перейдите в раздел **SQL Editor** (слева в меню)
4. Выполните запрос для проверки состояния:
```sql
SELECT * FROM conversation_state WHERE flow='fsm';
```
5. Если есть записи, выполните запрос для очистки:
```sql
DELETE FROM conversation_state WHERE flow='fsm';
```
6. Нажмите **Run** (или Ctrl+Enter)
7. Перезапустите бота на VPS:
```bash
sudo systemctl restart personal-assistant-bot
```

### Способ 2: Через Python скрипт на VPS

1. Подключитесь к VPS:
```bash
ssh your_user@your_vps_ip
```

2. Перейдите в директорию проекта:
```bash
cd /opt/personal-assistant-2
```

3. Создайте скрипт очистки:
```bash
cat > clear_fsm.py << 'EOF'
import asyncio
import asyncpg
import os

async def clear_fsm():
    # Загружаем DATABASE_URL из env файла
    db_url = None
    try:
        with open('/etc/personal-assistant-bot.env', 'r') as f:
            for line in f:
                if line.startswith('DATABASE_URL='):
                    db_url = line.split('=', 1)[1].strip()
                    break
    except Exception as e:
        print(f"❌ Ошибка чтения env файла: {e}")
        return
    
    if not db_url:
        print("❌ DATABASE_URL не найден в /etc/personal-assistant-bot.env")
        return
    
    print(f"🔗 Подключение к Supabase...")
    conn = await asyncpg.connect(db_url)
    try:
        # Проверяем текущее состояние
        rows = await conn.fetch("SELECT chat_id, step, updated_at FROM conversation_state WHERE flow='fsm'")
        if rows:
            print(f"📋 Найдено {len(rows)} застрявших FSM состояний:")
            for row in rows:
                print(f"  - chat_id={row['chat_id']}, step={row['step']}, updated={row['updated_at']}")
        else:
            print("✅ Застрявших FSM состояний не найдено")
            return
        
        # Очищаем
        result = await conn.execute("DELETE FROM conversation_state WHERE flow='fsm'")
        print(f"✅ Очищено FSM состояние: {result}")
    except Exception as e:
        print(f"❌ Ошибка: {e}")
    finally:
        await conn.close()

if __name__ == '__main__':
    asyncio.run(clear_fsm())
EOF
```

4. Запустите скрипт:
```bash
python3 clear_fsm.py
```

5. Удалите скрипт:
```bash
rm clear_fsm.py
```

6. Перезапустите бота:
```bash
sudo systemctl restart personal-assistant-bot
```

### Способ 3: Через psql на VPS (если установлен PostgreSQL клиент)

1. Подключитесь к VPS:
```bash
ssh your_user@your_vps_ip
```

2. Получите DATABASE_URL:
```bash
grep DATABASE_URL /etc/personal-assistant-bot.env
```

3. Подключитесь к Supabase через psql:
```bash
psql "postgresql://postgres:[password]@[host].supabase.co:5432/postgres"
```
(Замените на ваш DATABASE_URL)

4. Выполните SQL команды:
```sql
-- Проверка
SELECT * FROM conversation_state WHERE flow='fsm';

-- Очистка
DELETE FROM conversation_state WHERE flow='fsm';

-- Выход
\q
```

5. Перезапустите бота:
```bash
sudo systemctl restart personal-assistant-bot
```

## Проверка

После очистки состояния попробуйте:
1. Отправить боту команду `/start` или `/menu`
2. Перейти в раздел "Команда"
3. Открыть карточку проблемного сотрудника

Карточка должна открыться нормально.

## Если проблема сохраняется

Проверьте логи бота:
```bash
sudo journalctl -u personal-assistant-bot -n 100 --no-pager
```

Ищите ошибки связанные с этим сотрудником. Пришлите логи для дальнейшей диагностики.
