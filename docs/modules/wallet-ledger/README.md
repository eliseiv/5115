# Module: Wallet / Ledger

- Статус: Реализован
- Ответственность: баланс кредитов, атомарные идемпотентные списания, история транзакций, начисления (grant).

## Документы
- [00-overview.md](00-overview.md) · [01-context.md](01-context.md) · [02-api-contracts.md](02-api-contracts.md) · [03-architecture.md](03-architecture.md) · [04-data-model.md](04-data-model.md) · [05-events.md](05-events.md) · [06-rbac.md](06-rbac.md) · [07-implementation-phases.md](07-implementation-phases.md) · [09-testing.md](09-testing.md)

## DoD
Списание атомарно и идемпотентно (AC-3); отрицательный баланс невозможен; повторный idempotency key (поле `requestId`; для chat-debit = `messageStepId`) не списывает повторно; каждое списание в audit.

## Changelog
- 2026-05-21: bootstrap (architect).
- 2026-05-21: ADR-006 — семантика amount (целые кредиты): consume `amount=1`, grant фикс. пакета `SUBSCRIPTION_CREDITS_PER_PERIOD`. meta хранит usage для аудита, на amount не влияет.
- 2026-05-21: разведены `requestId` (gateway correlation) и `messageStepId` (billing idempotency key для chat-debit). Поле контракта `/wallet/consume` сохраняет имя `requestId`; Orchestrator передаёт туда `messageStepId` (ADR-005/ADR-006).
- 2026-05-21: реализован (backend), тесты зелёные, ревью пройдено. Код: `src/app/wallet/service.py` (consume/grant, атомарность + idempotency).
- 2026-06-01: `grant` получил HTTP-обёртку через Admin-модуль (`POST /v1/admin/wallet/grant`, изолированный `X-Admin-Token`, [ADR-009](../../adr/ADR-009-admin-token-auth.md)); grant-логика не дублируется, обёртка добавляет audit `admin_grant`. Сигнатура grant подтверждена по коду: `grant(user_id, amount, idempotency_key, meta, reason)`, идемпотентна по `(user_id, idempotency_key)`. Scope backend (новый модуль admin).
- 2026-05-25: live-e2e — `consume` валидирует существование/принадлежность `sessionId` (`404 session_not_found` / `403`) до FK-зависимых операций, чтобы прямой вызов с несуществующим `sessionId` не падал `500` (FK-violation на `audit_logs.session_id`). См. [02-api-contracts.md](02-api-contracts.md). Scope backend.
