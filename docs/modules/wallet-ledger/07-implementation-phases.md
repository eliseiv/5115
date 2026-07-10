# Wallet / Ledger — Implementation Phases

| Phase | Задача | Зависит от |
|---|---|---|
| WL-1 | Модели + миграции wallets/ledger_transactions, CHECK, unique index. | DB |
| WL-2 | `consume` атомарный идемпотентный (ADR-005) + insufficient handling. | WL-1 |
| WL-3 | `GET /v1/wallet` (balance + lastTransactions). | WL-1 |
| WL-4 | `grant` (внутренний) + идемпотентность. | WL-1 |
| WL-5 | audit billing_debit/credit + метрика wallet_debit_total. | WL-2, Audit |

> Wallet принимает готовый `amount`. Семантика: `consume` `amount=1` (1 кредит = 1 сообщение), `grant` `amount=SUBSCRIPTION_CREDITS_PER_PERIOD` на период подписки — [ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md).
