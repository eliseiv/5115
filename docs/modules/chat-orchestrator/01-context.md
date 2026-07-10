# Chat Orchestrator — Context

## Зависимости
| Зависит от | Зачем |
|---|---|
| Policy Engine | решение allow/blocked перед генерацией |
| BYOK Service | plaintext ключ для mode=byok (in-memory, на время вызова) |
| Wallet | списание кредитов после генерации (mode=credits) |
| Audit | запись шагов, tool lifecycle, мутирующих действий |
| Anthropic API | генерация (messages API + prompt caching) |
| PostgreSQL | chat_sessions, chat_steps, tool_calls |

## Кто зависит
- API Gateway маршрутизирует `/v1/chat/*` сюда.

## Связанные ADR
- [ADR-002](../../adr/ADR-002-access-policy-state-machine.md) — вызов Policy.
- [ADR-004](../../adr/ADR-004-blocked-http-200.md) — status=blocked / 200.
- [ADR-005](../../adr/ADR-005-idempotency-ledger.md) — идемпотентность tool-result и списания; `messageStepId` как billing-ключ (vs gateway `requestId`).

## Открытые вопросы / решения
- [Q-001-1](../../99-open-questions.md) — TTL сессии.
- [ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md) — биллинг credits-debit: 1 кредит = 1 сообщение, 1 списание на message-шаг (закрывает Q-004-1).
