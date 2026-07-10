# Wallet / Ledger — Data Model

Владеет: `wallets`, `ledger_transactions`. Полные DDL — [03-data-model.md](../../03-data-model.md).

## wallets
- `balance BIGINT >= 0` (CHECK `ck_wallets_balance_nonneg`).
- Один writer — этот модуль.

## ledger_transactions
- `type` ∈ {credit, debit}; `amount > 0`, **целые кредиты** (BIGINT, без дробей).
- `debit` для `mode=credits`: `amount=1` (1 кредит = 1 сообщение, [ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md)).
- `credit` при подписке: `amount=SUBSCRIPTION_CREDITS_PER_PERIOD` (дефолт 1000) на период (ADR-006).
- Идемпотентность: `ux_ledger_idempotency (user_id, idempotency_key)`.
- История: `ix_ledger_user_created (user_id, created_at DESC)`.
- `meta` JSONB — usage(inputTokens/outputTokens/model)/model для аудита, без секретов. На `amount` не влияет.

## Инварианты
- Append-only по смыслу: транзакции не редактируются/удаляются (баланс — производное состояние).
- `idempotency_key` для credits-debit (`consume`) = `messageStepId` (единый на пользовательский message-шаг, включая все tool-раунды и re-entry; передаётся Orchestrator в публичное поле `requestId` контракта `/wallet/consume`). **Не** gateway correlation `requestId`. Для grant = `transactionId` периода подписки.
