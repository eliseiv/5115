# Policy Engine — Context

## Зависимости (read-only)
| Зависит от | Зачем |
|---|---|
| Subscription repo | status, expiresAt |
| Wallet repo | creditsBalance |
| BYOK repo | enabled, key_status |
| users repo | trial_used |

## Кто зависит
- Chat Orchestrator (перед каждой генерацией).
- API Gateway → `/v1/policy/effective`.

## Связанные ADR
- [ADR-002](../../adr/ADR-002-access-policy-state-machine.md), [ADR-004](../../adr/ADR-004-blocked-http-200.md).

## Инвариант консистентности (AC-6)
`/policy/effective` и `/chat/run` используют **одну и ту же** функцию `evaluate`. `reasons[]` в effective = объединение blockReason для credits- и byok-режимов.
