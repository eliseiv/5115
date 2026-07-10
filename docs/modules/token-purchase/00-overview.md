# Token Purchase — Overview

## Назначение
Дизайн «Get More Tokens»: разовая покупка пакетов токенов (1500/600/250/100 за деньги), отдельно от подписки. Backend обрабатывает StoreKit **consumable** транзакцию и начисляет соответствующее число кредитов.

## Scope ([ADR-015](../../adr/ADR-015-consumable-token-iap.md))
- `POST /v1/tokens/purchase` — приём подписанной consumable-транзакции, верификация (общий verifier с subscription), маппинг `productId → credits`, идемпотентный grant.
- `GET /v1/tokens/products` (опц.) — каталог пакетов (productId → credits) для отображения цен/количеств клиентом (цены — из StoreKit, backend отдаёт маппинг credits). Дефолт: включить, читает `TOKEN_PRODUCTS`.

## Out of scope
- Подписка (модуль subscription, не меняется).
- Возвраты/refund consumable (на старте — не обрабатывается отдельно; consumable обычно не возвращается; при необходимости — отдельный проход).
- Хранение «токенов» как отдельной сущности — «токен» = кредит по `TOKEN_PRODUCTS` ([ADR-015](../../adr/ADR-015-consumable-token-iap.md)).

## Бизнес-правила
- BR-TP-1: число кредитов определяется **server-side** по `productId` (`TOKEN_PRODUCTS`), не из тела клиента (анти-подделка количества).
- BR-TP-2: grant идемпотентен по consumable `transactionId` (`ux_ledger_idempotency`); повторная отправка не начисляет повторно ([ADR-005](../../adr/ADR-005-idempotency-ledger.md)).
- BR-TP-3: `ledger_transactions.meta.source = "token_purchase"`, `meta.productId` — для аудита/истории; отличает от subscription grant.
- BR-TP-4: **покупка требует активной подписки** ([Q-015-1](../../99-open-questions.md) Closed = вариант B): policy-guard `subscription.status == active` **до** grant; нет активной подписки → `403 subscription_required`, ledger не пишется. Покупка = докупка сверх месячного пакета подписки. [ADR-002](../../adr/ADR-002-access-policy-state-machine.md) без изменений.
- BR-TP-5: неизвестный `productId` → `422`; невалидная/поддельная транзакция → `422`/`400` (как subscription).
