# Chat Orchestrator — Overview

## Scope
- `/v1/chat/run` — старт/продолжение агентного шага.
- `/v1/chat/tool-result` — приём результата локального tool и продолжение.
- Вызов Policy Engine **перед каждой** генерацией.
- Вызов Anthropic messages API с prompt caching и определением tools.
- Управление tool-loop state: создание `tool_calls`, проверка принадлежности, идемпотентность.
- Реконструкция контекста сессии из `chat_steps`.
- При `mode=credits`: инициирование списания через Wallet после генерации.
- При `mode=byok`: получение plaintext ключа от BYOK Service на время вызова.
- Запись `chat_steps`, аудит шагов и tool lifecycle.

## Out of scope
- Решение о доступе (Policy Engine).
- Списание/баланс (Wallet).
- Исполнение iOS tools (делает клиент).
- Шифрование/хранение BYOK (BYOK Service).

## Поведение по status
- `assistant_message` — финальный текст шага.
- `tool_call` — Claude запросил tool; backend возвращает строго типизированный payload, ждёт `/chat/tool-result`.
- `blocked` — Policy Engine отказал; `blockReason` обязателен; HTTP 200 (ADR-004).
