# Module: Token Purchase (consumable IAP)

- Статус: **Реализован (MVP); требует доработки policy-guard** ([Q-015-1](../../99-open-questions.md) Closed = вариант B — добавить проверку активной подписки перед grant). Перенесён из Спринта 3 в MVP по решению пользователя ([figma-gap-analysis.md §MVP-scope](../../figma-gap-analysis.md#mvp-scope-решение-пользователя-2026-06-02)).
- Ответственность: обработка разовой (consumable) StoreKit-покупки пакетов токенов → идемпотентный grant кредитов **для активных подписчиков** ([ADR-015](../../adr/ADR-015-consumable-token-iap.md)). Докупка сверх месячного пакета подписки.
- ✅ **[Q-015-1](../../99-open-questions.md) Closed (2026-06-02, вариант B):** покупка токенов **требует активной подписки**. Без активной подписки → `403 subscription_required` (policy-guard **до** grant). Сохраняет §2 ТЗ и [ADR-002](../../adr/ADR-002-access-policy-state-machine.md) без изменений; устраняет «мёртвый» баланс. Backend-доработка: см. [07-implementation-phases.md Phase 4](07-implementation-phases.md).

## Документы
- [00-overview.md](00-overview.md)
- [01-context.md](01-context.md)
- [02-api-contracts.md](02-api-contracts.md)
- [03-architecture.md](03-architecture.md)
- [06-rbac.md](06-rbac.md)
- [07-implementation-phases.md](07-implementation-phases.md)
- [09-testing.md](09-testing.md)

> Без новой таблицы: переиспользует `ledger_transactions` (`type=credit`, `meta.source=token_purchase`) и Wallet.grant ([ADR-015](../../adr/ADR-015-consumable-token-iap.md)). Verifier — общий с subscription (включая `STOREKIT_TEST_MODE`, [TD-007](../../100-known-tech-debt.md)).

## DoD (выполнено, MVP)
- ✅ `POST /v1/tokens/purchase` — верификация consumable-транзакции (reuse StoreKit verifier, включая `STOREKIT_TEST_MODE`), извлечение `transactionId`/`productId`, server-side маппинг `productId → credits` (`TOKEN_PRODUCTS`), идемпотентный grant по `transactionId` через `WalletService.grant` (`type=credit`, `meta.source=token_purchase`, `productId`). Без миграции (переиспользует `ledger_transactions`).
- ✅ `GET /v1/tokens/products` — каталог пакетов (`productId → credits`) из `TOKEN_PRODUCTS`; цены отображает клиент из StoreKit.
- ✅ Не ломает subscription grant (ADR-006) и инвариант «1 кредит = 1 сообщение» при списании; идемпотентность ledger ([ADR-005](../../adr/ADR-005-idempotency-ledger.md)) сохранена.
- ⏳ **Policy-guard «только активная подписка» ([Q-015-1](../../99-open-questions.md) = вариант B) — к реализации в backend** ([07-implementation-phases.md Phase 4](07-implementation-phases.md)): проверка `subscription.status == active` до `WalletService.grant`; нет подписки → `403 subscription_required`. Устраняет «мёртвый» баланс. До этой доработки покупка ошибочно доступна без подписки.

## Changelog
- 2026-06-02: bootstrap модуля (architect, Figma-gap). [ADR-015](../../adr/ADR-015-consumable-token-iap.md). См. [figma-gap-analysis.md](../../figma-gap-analysis.md).
- 2026-06-02: backend-реализация — `src/app/token_purchase/service.py`, роутер `src/app/api_gateway/routers/token_purchase.py` (`POST /v1/tokens/purchase`, `GET /v1/tokens/products`), конфиг `TOKEN_PRODUCTS`, метрика `token_purchase_total`. Без миграции (переиспользует `ledger_transactions`).
- 2026-06-02 (Update-sync): статус → **Реализован (MVP)**. Зафиксирован продуктовый блокер [Q-015-1](../../99-open-questions.md) (покупка без подписки не разблокирует потребление — противоречие policy ADR-002), выявлен qa+reviewer. Ответ — `creditsAdded`/`newBalance`/`transactionId` (см. [02-api-contracts.md](02-api-contracts.md)).
- 2026-06-02 (Q-015-1 Closed): решение пользователя = **вариант B** (покупка требует активной подписки, докупка сверх месячного пакета). Зафиксирован обязательный policy-guard перед grant (`403 subscription_required` без активной подписки), [ADR-015 §Доступность](../../adr/ADR-015-consumable-token-iap.md) обновлён, [ADR-002](../../adr/ADR-002-access-policy-state-machine.md) без изменений. К backend — [Phase 4](07-implementation-phases.md).
