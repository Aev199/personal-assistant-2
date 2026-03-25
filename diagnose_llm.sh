#!/bin/bash
echo "=== ДИАГНОСТИКА LLM ==="
echo ""

# 1. Проверка процесса
echo "1. Процесс бота:"
BOT_PID=$(pgrep -f "bot.py" | head -1)
if [ -n "$BOT_PID" ]; then
    echo "  ✓ PID: $BOT_PID"
    
    # Проверка переменных
    echo ""
    echo "2. Переменные в процессе:"
    GEMINI_KEY=$(sudo cat /proc/$BOT_PID/environ 2>/dev/null | tr '\0' '\n' | grep "^GEMINI_API_KEY=" | cut -d'=' -f2-)
    if [ -n "$GEMINI_KEY" ]; then
        echo "  ✓ GEMINI_API_KEY найден (длина: ${#GEMINI_KEY})"
        echo "    Начало: ${GEMINI_KEY:0:10}..."
    else
        echo "  ✗ GEMINI_API_KEY НЕ найден в процессе!"
    fi
else
    echo "  ✗ Процесс не найден"
    exit 1
fi

# 2. Проверка .env
echo ""
echo "3. Проверка .env файла:"
if [ -f ".env" ]; then
    echo "  ✓ .env существует"
    ENV_KEY=$(grep "^GEMINI_API_KEY=" .env | cut -d'=' -f2- | tr -d '"' | tr -d "'")
    if [ -n "$ENV_KEY" ]; then
        echo "  ✓ GEMINI_API_KEY в .env (длина: ${#ENV_KEY})"
        echo "    Начало: ${ENV_KEY:0:10}..."
        
        # Проверка формата
        if [[ "$ENV_KEY" =~ [[:space:]] ]]; then
            echo "  ✗ ПРОБЛЕМА: Ключ содержит пробелы!"
        fi
        if [[ "$ENV_KEY" == AIzaSy* ]]; then
            echo "  ✓ Формат корректный (начинается с AIzaSy)"
        else
            echo "  ✗ ПРОБЛЕМА: Ключ не начинается с AIzaSy"
        fi
    else
        echo "  ✗ GEMINI_API_KEY НЕ найден в .env"
    fi
else
    echo "  ✗ .env не существует"
fi

# 3. Сравнение
echo ""
echo "4. Сравнение ключей:"
if [ -n "$ENV_KEY" ] && [ -n "$GEMINI_KEY" ]; then
    if [ "$ENV_KEY" = "$GEMINI_KEY" ]; then
        echo "  ✓ Ключи совпадают"
    else
        echo "  ✗ ПРОБЛЕМА: Ключи НЕ совпадают!"
    fi
elif [ -n "$ENV_KEY" ] && [ -z "$GEMINI_KEY" ]; then
    echo "  ✗ ПРОБЛЕМА: Ключ в .env, но НЕ загружен в процесс"
    echo "    Решение: sudo systemctl restart your-bot-service"
fi

# 4. Тест API
echo ""
echo "5. Тест Gemini API:"
TEST_KEY="${GEMINI_KEY:-$ENV_KEY}"
if [ -n "$TEST_KEY" ]; then
    HTTP_CODE=$(curl -s -w "%{http_code}" -o /dev/null -m 10 \
      -X POST "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash-exp:generateContent" \
      -H "x-goog-api-key: $TEST_KEY" \
      -d '{"contents":[{"parts":[{"text":"test"}]}]}')
    
    if [ "$HTTP_CODE" = "200" ]; then
        echo "  ✓ API работает! Ключ валидный"
    elif [ "$HTTP_CODE" = "401" ]; then
        echo "  ✗ Ошибка 401: Ключ НЕВЕРНЫЙ"
    elif [ "$HTTP_CODE" = "400" ]; then
        echo "  ⚠ Ошибка 400: Попробуйте модель gemini-1.5-flash"
    else
        echo "  ✗ Ошибка $HTTP_CODE"
    fi
else
    echo "  ✗ Ключ не найден для теста"
fi

# 5. Диагноз
echo ""
echo "=== ДИАГНОЗ ==="
if [ -z "$GEMINI_KEY" ]; then
    echo "❌ Ключ НЕ загружен в процесс бота"
    echo ""
    echo "Проверьте формат в .env:"
    echo "  cat .env | grep GEMINI_API_KEY"
    echo ""
    echo "Должно быть БЕЗ пробелов и кавычек:"
    echo "  GEMINI_API_KEY=AIzaSy..."
    echo ""
    echo "Затем перезапустите:"
    echo "  sudo systemctl restart your-bot-service"
elif [ "$HTTP_CODE" = "401" ]; then
    echo "❌ Ключ загружен, но НЕВЕРНЫЙ"
    echo ""
    echo "Получите новый ключ:"
    echo "  https://aistudio.google.com/app/apikey"
elif [ "$HTTP_CODE" = "200" ]; then
    echo "✓ Всё настроено корректно!"
    echo ""
    echo "Если бот всё равно не работает:"
    echo "  journalctl -u your-bot-service -f | grep -i gemini"
fi
echo ""
