# Subscription — Context

## Зависимости
| Зависит от | Зачем |
|---|---|
| Apple App Store Server API | верификация транзакции / получение статуса |
| PostgreSQL | subscriptions |
| Wallet | grant кредитов при активации |
| Audit | запись изменений подписки |

## Кто зависит
- Policy Engine (read статуса).
- API Gateway (`/v1/subscription/sync`).

## Открытые вопросы / решения
- [Q-007-1](../../99-open-questions.md) — sandbox/prod, refund/revocation.
- [ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md) — начисление кредитов: фикс. пакет на период (закрывает Q-006-1).
