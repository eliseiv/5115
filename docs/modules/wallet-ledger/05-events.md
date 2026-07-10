# Wallet / Ledger — Events

Синхронные audit-записи (без брокера, см. [chat-orchestrator/05-events.md](../chat-orchestrator/05-events.md)).

| event_type | Когда | payload |
|---|---|---|
| `billing_debit` | успешное списание | `{ledgerTxId, amount, newBalance, sessionId, model}` |
| `billing_credit` | начисление (grant) | `{ledgerTxId, amount, newBalance, reason}` |

Метрика: `wallet_debit_total{result=success|fail}`.
