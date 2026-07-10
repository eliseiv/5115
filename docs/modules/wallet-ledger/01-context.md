# Wallet / Ledger — Context

## Зависимости
| Зависит от | Зачем |
|---|---|
| PostgreSQL | wallets, ledger_transactions |
| Audit | запись billing_debit / credit grant |

## Кто зависит
- Chat Orchestrator (`consume` при mode=credits).
- Policy Engine (read creditsBalance).
- Subscription (`grant` при активации плана).
- API Gateway (`GET /v1/wallet`).

## Связанные ADR / вопросы
- [ADR-005](../../adr/ADR-005-idempotency-ledger.md) — атомарность/идемпотентность.
- [ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md) — `consume` `amount=1` (1 кредит = 1 сообщение); `grant` фикс. пакета на период подписки. Закрывает Q-004-1, Q-006-1.
