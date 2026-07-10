# Subscription — Data Model

Владеет: `subscriptions`. Полный DDL — [03-data-model.md](../../03-data-model.md).

## subscriptions
- PK = `user_id` (одна активная запись на пользователя).
- `status` ∈ {active, expired, none}.
- `expires_at` — конец текущего периода (nullable для none).
- `ix_subscriptions_expires_at` — для фоновых проверок истечения.

## Связь с ledger
- Идемпотентность grant — `idempotency_key` = `sub-grant:<transactionId>` в `ledger_transactions`.

## Инварианты
- Только этот модуль пишет в `subscriptions`.
- Статус нормализуется при каждом sync.
