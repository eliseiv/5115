# Module: Subscription

- Статус: Реализован
- Ответственность: server-side верификация StoreKit транзакций, нормализация статуса подписки, grant кредитов при активации.

## Документы
- [00-overview.md](00-overview.md) · [01-context.md](01-context.md) · [02-api-contracts.md](02-api-contracts.md) · [03-architecture.md](03-architecture.md) · [04-data-model.md](04-data-model.md) · [06-rbac.md](06-rbac.md) · [07-implementation-phases.md](07-implementation-phases.md) · [09-testing.md](09-testing.md)

## DoD
StoreKit транзакция верифицируется server-side; статус/expiresAt/plan консистентны; истёкшая подписка корректно отражается в Policy.

## Changelog
- 2026-05-21: bootstrap (architect).
- 2026-05-21: ADR-006 — начисление кредитов: фикс. пакет `SUBSCRIPTION_CREDITS_PER_PERIOD` (дефолт 1000) на период, идемпотентно по transactionId. Закрыт Q-006-1.
- 2026-05-21: реализован (backend), тесты зелёные, ревью пройдено. Код: `src/app/subscription/service.py`, `src/app/subscription/storekit.py` (реальная JWS x5c chain+signature verification; fail-closed при незаданном `APPSTORE_ROOT_CERT_DIR`, Q-007-1).
