# Token Purchase — Architecture

## Размещение
Пакет `src/app/token_purchase/`: use-case `purchase_tokens` + роутер `/v1/tokens/*`. Verifier — общий с subscription (выделить в общий модуль `src/app/storekit/` или переиспользовать существующий verifier subscription без дублирования).

## Поток purchase
```
require_active_subscription(userId)                    # Q-015-1=B: SubscriptionService/policy; нет active -> 403 subscription_required
verify(transaction) -> { transactionId, productId }    # общий verifier (fail-closed; STOREKIT_TEST_MODE)
credits = TOKEN_PRODUCTS[productId]                     # неизвестный -> 422
Wallet.grant(credits, idempotency_key=transactionId,
             type=credit, meta={source:"token_purchase", productId})
-> { creditsAdded, newBalance }
```

## Проверка подписки (обязательная, [Q-015-1](../../99-open-questions.md) = вариант B)
- **Где:** в use-case `purchase_tokens`, **первым шагом — до verify и до `Wallet.grant`**. Источник истины статуса подписки — `SubscriptionService`/policy-слой (тот же, что питает `PolicyState.subscription_status`, [ADR-002](../../adr/ADR-002-access-policy-state-machine.md)); проверяем `subscription.status == active`.
- **Почему до verify:** fail-fast — не расходуем вызов App Store Server API и не пишем ledger для неподписанного пользователя.
- **Почему до grant:** policy-guard не должен ломать идемпотентность. Проверка подписки чисто read-only и предшествует единственной записи ledger (`Wallet.grant`). Если подписки нет — grant не вызывается, `ux_ledger_idempotency` не затрагивается.
- **Отказ:** HTTP `403`, тело `{ "code": "subscription_required", "message": "..." }`. Это операция пополнения, не генерация → `403`, а не `200+blocked` ([ADR-004](../../adr/ADR-004-blocked-http-200.md) применяется только к `/chat/*` и `/policy/effective`; см. [ADR-015 §Код ответа](../../adr/ADR-015-consumable-token-iap.md)).

## Идемпотентность ([ADR-005](../../adr/ADR-005-idempotency-ledger.md), [ADR-015](../../adr/ADR-015-consumable-token-iap.md))
- `idempotency_key = transactionId` (consumable). Unique-index `ux_ledger_idempotency (user_id, idempotency_key)` гарантирует один grant на транзакцию. Повтор → idempotent-ответ (`creditsAdded=0`).
- Пространства transactionId subscription и consumable не пересекаются (StoreKit), но даже при коллизии разные `meta.source`/контракты исключают неоднозначность; idempotency-инвариант ledger сохраняется.

## Разграничение с subscription
- Subscription verifier и grant — не меняются (ADR-006). Token-purchase — отдельный endpoint и отдельный путь grant с тем же Wallet API.
- Списание (`type=debit`) — не затрагивается; «1 кредит = 1 сообщение» в силе.

## Инварианты
- **Покупка только при активной подписке** ([Q-015-1](../../99-open-questions.md) = вариант B): grant не выполняется без `subscription.status == active`.
- Число кредитов — только из `TOKEN_PRODUCTS` (server-side), никогда из тела клиента.
- Wallet — единственный писатель ledger (инвариант сохранён).
- StoreKit payload не логируется.
