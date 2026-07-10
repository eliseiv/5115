# Module: Audit

- Статус: Реализован
- Ответственность: append-only журнал мутирующих tool-действий, billing trace, policy/byok/subscription изменений.

## Документы
- [00-overview.md](00-overview.md) · [01-context.md](01-context.md) · [02-api-contracts.md](02-api-contracts.md) · [03-architecture.md](03-architecture.md) · [04-data-model.md](04-data-model.md) · [06-rbac.md](06-rbac.md) · [07-implementation-phases.md](07-implementation-phases.md) · [09-testing.md](09-testing.md)

## DoD
Каждое мутирующее tool-действие и каждое списание фиксируется (AC-7); записи неизменяемы (append-only на уровне приложения); секреты не попадают в payload.

## Changelog
- 2026-05-21: bootstrap (architect).
- 2026-05-21: реализован (backend), тесты зелёные, ревью пройдено. Код: `src/app/audit/service.py`. Append-only TD-001 (только на уровне приложения) остаётся в силе.
