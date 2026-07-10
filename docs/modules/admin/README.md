# Module: Admin

- Статус: Реализован
- Ответственность: операторские/саппорт-действия над аккаунтами под изолированной admin-авторизацией. Начисление кредитов (`wallet/grant`), read-only просмотр кошелька, ручная активация/продление подписки (`subscription/grant`, [ADR-048](../../adr/ADR-048-admin-subscription-grant.md)).

## Документы
- [00-overview.md](00-overview.md)
- [01-context.md](01-context.md)
- [02-api-contracts.md](02-api-contracts.md)
- [03-architecture.md](03-architecture.md)
- [06-rbac.md](06-rbac.md)
- [07-implementation-phases.md](07-implementation-phases.md)
- [09-testing.md](09-testing.md)

## DoD
- `POST /v1/admin/wallet/grant` начисляет кредиты через существующий `WalletService.grant()`, идемпотентно по `idempotencyKey`, с обязательным `reason`; ответ `{newBalance, ledgerTxId, idempotentReplay}`.
- `GET /v1/admin/wallet/{userId}` отдаёт баланс + последние ledger-транзакции (read-only).
- `POST /v1/admin/subscription/grant` ([ADR-048](../../adr/ADR-048-admin-subscription-grant.md)) активирует/продлевает подписку (upsert `subscriptions` `status='active'`, `plan`, `expires_at`) **без** StoreKit-транзакции; срок = ровно одно из `expiresAt` (tz-aware, `>now()`) / `days` (>0); опц. начисление кредитов в том же запросе (дефолт `SUBSCRIPTION_CREDITS_PER_PERIOD`, явный `0` = без начисления) через `WalletService.grant`, идемпотентно по `idempotencyKey`; `404 user_not_found`; всё в одной транзакции; audit `admin_subscription_grant`.
- Авторизация — изолированный `X-Admin-Token` ([ADR-009](../../adr/ADR-009-admin-token-auth.md)), отдельная зависимость `require_admin`, не пересекается с пользовательским JWT, не запускает provisioning, не трогает trial.
- Аудит-событие `admin_grant` (actor=admin, reason, без секрета). Отдельный rate limit, strict validation, size-лимиты.

## Changelog
- 2026-06-01: bootstrap модуля (architect). Зафиксированы [ADR-009](../../adr/ADR-009-admin-token-auth.md) (admin-auth), контракты grant/get-wallet, RBAC, фазы, тесты. Scope backend.
- 2026-06-01: реализован backend (`src/app/api_gateway/routers/admin.py`, `src/app/admin/service.py`): `POST /v1/admin/wallet/grant` + `GET /v1/admin/wallet/{userId}` под `require_admin`/`X-Admin-Token`, audit `admin_grant`, отдельный rate limit. Отревьюен и протестирован — offline-сьют зелёный (455/455, вкл. e2e admin-grant/get-wallet). Статус → «Реализован».
- 2026-07-01: спроектирован [ADR-048](../../adr/ADR-048-admin-subscription-grant.md) — `POST /v1/admin/subscription/grant` (ручная активация подписки без StoreKit, опц. начисление кредитов). Обновлены 02-api-contracts / 03-architecture / 07-implementation-phases (фаза ADM-8) / 09-testing (сценарии subscription/grant: integration/policy-e2e/validation/security). Scope backend (ТЗ в ADR-048). Реализация — pending.
