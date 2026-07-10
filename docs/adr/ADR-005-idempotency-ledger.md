# ADR-005 — Атомарность и идемпотентность ledger и tool-result

- Статус: Accepted
- Дата: 2026-05-21

## Context
ТЗ §4.5 и AC-3: списание кредитов атомарно и идемпотентно, отрицательный баланс невозможен, повторный idempotency key (в публичном контракте `/wallet/consume` — поле `requestId (idempotency key)`) не списывает повторно. ТЗ §4.2 и AC-4: повторная отправка tool-result идемпотентна. iOS-клиент может ретраить запросы (сеть).

### Терминология (важно)
- **`requestId` (Gateway correlation id)** — per-HTTP-request идентификатор (`X-Request-Id`), авто-генерируемый Gateway на каждый HTTP-запрос для логов/трейсов. Жизненный цикл — один HTTP-запрос. **Не** является billing-ключом.
- **`messageStepId` (billing message-step id)** — идентификатор пользовательского message-шага. Генерируется Orchestrator в `/chat/run` при старте нового message-шага и переиспользуется всеми tool-раундами этого шага (включая re-entry из `/chat/tool-result`) вплоть до финального assistant_message. Это и есть ключ идемпотентности credits-debit.
- Публичное поле запроса `/wallet/consume` исторически называется `requestId (idempotency key)` (ТЗ §4.5). Для chat-debit Orchestrator передаёт в это поле значение `messageStepId`. Требование §4.5 «повторный requestId не списывает повторно» выполняется именно значением `messageStepId`. Внутренний gateway-correlation `requestId` в это поле НЕ передаётся.

## Decision

### Wallet consume (списание)
В одной транзакции БД (`SERIALIZABLE` или `READ COMMITTED` + блокировка строки):
```sql
BEGIN;
-- идемпотентность: попытка вставить ledger c unique (user_id, idempotency_key)
INSERT INTO ledger_transactions (..., idempotency_key) VALUES (...)
  ON CONFLICT (user_id, idempotency_key) DO NOTHING
  RETURNING id;
-- если конфликт (0 строк) -> вернуть существующую транзакцию и текущий баланс, НЕ списывать
-- если вставлено:
UPDATE wallets SET balance = balance - :amount, updated_at = now()
  WHERE user_id = :uid AND balance >= :amount;   -- AC-3: запрет отрицательного
-- если 0 строк обновлено -> откат, ошибка credits_empty/insufficient
COMMIT;
```
- `idempotency_key` = значение публичного поля `requestId` запроса `/wallet/consume`. Для credits-debit Orchestrator подставляет туда `messageStepId` (единый на весь message-шаг, включая все его tool-раунды и re-entry из `/chat/tool-result`). Для grant — `transactionId` периода подписки. Internal gateway correlation `requestId` сюда не попадает.
- `messageStepId` персистируется в `chat_steps.message_step_id` и `tool_calls.message_step_id`; при re-entry `/chat/tool-result` находит текущий `messageStepId` через `tool_calls.message_step_id` и переиспользует его на финальном debit.
- Двойная защита от отрицательного баланса: `WHERE balance >= :amount` + DB CHECK `balance >= 0`.
- Redis может хранить кратковременную метку in-flight для быстрого отсечения дублей, но **источник истины идемпотентности — unique index в PostgreSQL**.

### Tool-result идемпотентность
- `toolCallId` = `tool_calls.id`. Перед обработкой проверяется `tool_calls.session_id == sessionId` (принадлежность, иначе `403/404`).
- Если `tool_calls.status == completed` (повторная отправка) → не пересылать в Anthropic повторно; вернуть сохранённый следующий ответ шага (из `chat_steps`) идемпотентно.
- Переход `pending → completed/errored` атомарен (`UPDATE ... WHERE status='pending'`).

### Trial (BR-1, связано)
```sql
UPDATE users SET trial_used = TRUE WHERE id = :uid AND trial_used = FALSE;
-- 1 строка -> trial выдан; 0 строк -> уже использован (block trial_used)
```

## Consequences
- (+) Корректность под конкурентные ретраи без распределённых локов (одна БД).
- (+) Идемпотентность гарантирована схемой, а не только кодом.
- (−) При совпадении idempotency key (поле `requestId` consume; для chat-debit это `messageStepId`) с **другим** payload (amount/meta) — конфликт; политика: вернуть `409`, не списывать. Orchestrator обязан использовать один и тот же `messageStepId` на весь message-шаг и не переиспользовать его между разными логическими операциями.

## Alternatives
- Идемпотентность только в Redis — отвергнуто: потеря Redis = двойное списание.
- Application-level lock без транзакции БД — отвергнуто: гонки при нескольких репликах API.
