# Module: Policy Engine

- Статус: Реализован
- Ответственность: единый источник истины правил доступа (trial/subscription/credits/byok). Чистая функция решения. Питает `/v1/policy/effective` и `/v1/chat/run`.

## Документы
- [00-overview.md](00-overview.md) · [01-context.md](01-context.md) · [02-api-contracts.md](02-api-contracts.md) · [03-architecture.md](03-architecture.md) · [04-data-model.md](04-data-model.md) · [06-rbac.md](06-rbac.md) · [07-implementation-phases.md](07-implementation-phases.md) · [09-testing.md](09-testing.md)

## DoD
`/policy/effective` консистентен с `/chat/run` (AC-6); все blockReason покрыты; state machine из ADR-002 реализована и протестирована параметризованно.

## Changelog
- 2026-05-21: bootstrap (architect).
- 2026-05-21: реализован (backend), тесты зелёные, ревью пройдено. Код: `src/app/policy/engine.py` (чистая функция), `src/app/policy/loader.py` (state из репозиториев).
