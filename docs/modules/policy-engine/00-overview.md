# Policy Engine — Overview

## Scope
- Чистая функция `evaluate(state, mode) -> Decision` (allow / blocked+blockReason).
- `/v1/policy/effective` — агрегированные эффективные права для UI.
- Сбор `state` из репозиториев Subscription / Wallet / BYOK / users(trial_used).

## Out of scope
- Мутации (trial_used переключает Orchestrator/use-case при фактической выдаче, не Policy).
- Списание, генерация.

## Источник правил
[ADR-002](../../adr/ADR-002-access-policy-state-machine.md) (state machine, BR-1..BR-5, порядок проверок).
