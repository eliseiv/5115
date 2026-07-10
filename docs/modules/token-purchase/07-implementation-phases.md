# Token Purchase — Implementation Phases

Спринт 3. Без новой таблицы (использует `ledger_transactions`/Wallet). Зависит от существующего StoreKit verifier и Wallet.grant.

1. **Phase 1 — verifier reuse:** выделить/переиспользовать общий StoreKit verifier (subscription + token-purchase), без дублирования.
2. **Phase 2 — config:** `TOKEN_PRODUCTS` (productId→credits) в env ([07-deployment.md](../../07-deployment.md)).
3. **Phase 3 — endpoint:** `POST /v1/tokens/purchase` (verify → map → idempotent grant), `GET /v1/tokens/products`.
4. **Phase 4 — policy-guard «только активная подписка» ([Q-015-1](../../99-open-questions.md) Closed = вариант B, ОБЯЗАТЕЛЬНО):** в use-case `purchase_tokens` добавить проверку `subscription.status == active` через `SubscriptionService`/policy **первым шагом, до verify и до `WalletService.grant`**. Нет активной подписки → `403 {code: "subscription_required"}`, без записи ledger. Не менять идемпотентность (проверка read-only, до единственной записи) и существующую логику verify/grant. Покрыть тестами: подписан → grant ок; не подписан → `403`, ledger пуст; идемпотентный повтор подписчика → `creditsAdded=0`. (Ранее был опционален под дефолт Q-015-1 — теперь обязателен.)
