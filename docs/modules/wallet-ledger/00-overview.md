# Wallet / Ledger — Overview

## Scope
- `GET /v1/wallet` — баланс + последние транзакции.
- `POST /v1/wallet/consume` — атомарное идемпотентное списание.
- Внутренний `grant(userId, amount, meta)` — начисление кредитов (тип `credit`), вызывается Subscription при активации/продлении плана фикс. пакетом на период ([ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md)).
- Единственный writer для `wallets` и `ledger_transactions`.

## Out of scope
- Решение, можно ли списывать (Policy Engine).
- Определение `amount` (Orchestrator передаёт готовое значение: `amount=1` для credits-debit по [ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md); Wallet не конвертирует usage).
- Публичная покупка кредитов (out of scope bootstrap).

## Ключевые гарантии
- Атомарность + идемпотентность ([ADR-005](../../adr/ADR-005-idempotency-ledger.md)).
- `balance >= 0` всегда (DB CHECK + условие в UPDATE).
