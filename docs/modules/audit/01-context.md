# Audit — Context

## Зависимости
| Зависит от | Зачем |
|---|---|
| PostgreSQL | audit_logs |

## Кто зависит (вызывают record)
- Chat Orchestrator — policy_decision, chat_step, tool lifecycle, мутирующие tool-действия.
- Wallet — billing_debit / billing_credit.
- BYOK — byok_change.
- Subscription — subscription_change.

## Связанные документы
- [chat-orchestrator/05-events.md](../chat-orchestrator/05-events.md) — каталог событий.
- [TD-001](../../100-known-tech-debt.md) — append-only только на app-уровне.
