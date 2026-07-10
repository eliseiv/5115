# Wallet / Ledger — API Contracts

## GET /v1/wallet
### Response (200)
```json
{
  "balance": 0,
  "lastTransactions": [
    { "id": "uuid", "type": "credit|debit", "amount": 0, "createdAt": "ISO8601", "meta": {} }
  ]
}
```
- `lastTransactions` — последние N (дефолт 20), по `created_at DESC`.
- `meta` — без секретов (usage/model).

## POST /v1/wallet/consume
### Request
```json
{
  "userId": "uuid",
  "sessionId": "uuid",
  "requestId": "string (idempotency key)",
  "amount": 1,
  "meta": { "usage": {}, "model": "string" }
}
```
- `amount` > 0, **целые кредиты** (BIGINT, без дробей). При штатном вызове из Orchestrator для `mode=credits` всегда `amount=1` (1 кредит = 1 сообщение, [ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md)). Сам контракт допускает любое целое > 0.
- `meta.usage` (inputTokens/outputTokens/model) хранится для аудита и **не влияет** на `amount`.
- `requestId` — **публичное имя поля идемпотентности** этого контракта (сохранено из ТЗ §4.5 «повторный requestId не списывает повторно»). Для chat-debit Orchestrator передаёт сюда `messageStepId` — идентификатор пользовательского message-шага, единый на все его tool-раунды и re-entry из `/chat/tool-result` ([ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md)). Используется как `idempotency_key` debit. **Это не gateway correlation `requestId`** (`X-Request-Id`, per-HTTP-request, логи/трейсы — см. [api-gateway/02-api-contracts.md](../api-gateway/02-api-contracts.md)); тот в это поле не передаётся.

### Response (200)
```json
{ "newBalance": 0, "ledgerTxId": "uuid" }
```

### Правила (ADR-005)
- **Валидация `sessionId` (робастность, защита от мусорного ввода).** До любой FK-зависимой операции (списание + audit `billing_debit`, который пишет `audit_logs.session_id`) backend проверяет, что `sessionId` существует в `chat_sessions` **и принадлежит `userId`**. Несуществующий `sessionId` → `404 {error.code:"session_not_found"}`; `sessionId` принадлежит другому пользователю → `403`. Это предотвращает `500` из-за FK-violation на `audit_logs.session_id` при прямом вызове с несуществующим `sessionId`. Проверка обязательна и для штатного пути (Orchestrator), и для прямого клиентского вызова. Проверка выполняется до проверки идемпотентности/баланса.
- Атомарное списание в одной транзакции БД.
- Запрет отрицательного баланса: `UPDATE ... WHERE balance >= amount` + DB CHECK.
- Повторное значение поля `requestId` (для chat-debit — тот же `messageStepId`, тот же userId) → не списывает повторно, возвращает существующий `ledgerTxId` и текущий `newBalance` (идемпотентно, 200).
- То же значение `requestId`/`messageStepId` с другим `amount`/`meta` → `409` (конфликт), без списания.
- Недостаточно средств → бизнес-ошибка: возвращается `409`/специфичный код? — **техническая семантика**: `consume` вызывается Orchestrator уже после allow от Policy; если баланс изменился и стал недостаточен — `409 {error.code:"insufficient_credits"}`, Orchestrator транслирует в `blocked/credits_empty`.

> Контракт `consume` — внутренний биллинговый; вызывается Orchestrator, не клиентом напрямую (хотя проходит через Gateway с тем же auth). Прямой клиентский вызов допустим, но штатный путь — через `/chat/run`.

## Внутренний grant
```
grant(userId, amount>0, idempotency_key, meta, reason) -> newBalance, ledgerTxId, idempotentReplay
```
Создаёт `ledger_transactions(type=credit)`, увеличивает баланс. Идемпотентен по `(user_id, idempotency_key)`, пишет audit `billing_credit` (принимает `reason`). Реализация: `src/app/wallet/service.py:174`.

Вызывается:
- **Subscription** при активации/продлении плана: фиксированный пакет `SUBSCRIPTION_CREDITS_PER_PERIOD` (дефолт 1000) кредитов на период, идемпотентность по `transactionId` периода ([ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md)).
- **Admin** через `POST /v1/admin/wallet/grant` (изолированная admin-авторизация, [ADR-009](../../adr/ADR-009-admin-token-auth.md)): ручное начисление кредитов, идемпотентность по `idempotencyKey` из тела, обязательный `reason`. См. [modules/admin/02-api-contracts.md](../admin/02-api-contracts.md). Admin-обёртка дополнительно пишет audit `admin_grant`; саму grant-логику **не** дублирует.
