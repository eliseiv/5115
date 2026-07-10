# Token Purchase — API Contracts

JWT, владелец = `sub`. Статус: **Реализован (MVP); требует доработки policy-guard** ([Q-015-1](../../99-open-questions.md) Closed = вариант B). Заголовок `Authorization: Bearer <JWT>`, тег `Tokens`.

> ✅ **[Q-015-1](../../99-open-questions.md) Closed (2026-06-02, вариант B):** покупка токенов **требует активной подписки** (докупка сверх месячного пакета). Без активной подписки → `403 subscription_required` **до** начисления. [ADR-002](../../adr/ADR-002-access-policy-state-machine.md) без изменений. Backend-доработка: добавить policy-guard перед `WalletService.grant` (см. [03-architecture.md](03-architecture.md), [07-implementation-phases.md](07-implementation-phases.md)).

## POST /v1/tokens/purchase
Обработка consumable-покупки пакета токенов.

### Request
```json
{
  "userId": "uuid",
  "transaction": { "...StoreKit consumable transaction payload (signed)..." }
}
```
- `transaction` — подписанный StoreKit payload consumable-покупки (JWS / App Store Server API). Не логируется (redaction, как subscription).

### Поведение ([ADR-015](../../adr/ADR-015-consumable-token-iap.md))
1. **Policy-guard (обязателен, [Q-015-1](../../99-open-questions.md) = вариант B):** проверить активную подписку (`subscription.status == active`). Нет активной подписки → **`403`** `{ "code": "subscription_required", "message": "..." }`. Кредиты **не** начисляются, ledger не пишется. Проверка — **до** verify/grant (fail-fast, не тратим вызов App Store API на неподписанных).
2. Верификация транзакции общим verifier'ом (реальная Apple JWS / `STOREKIT_TEST_MODE` для e2e). Невалидная → `422`/`400`.
3. Извлечь `transactionId`, `productId`.
4. Маппинг `productId → credits` через server-side `TOKEN_PRODUCTS`. Неизвестный `productId` → `422`.
5. `Wallet.grant(credits, idempotency_key=transactionId, type=credit, meta={source:"token_purchase", productId})`. Идемпотентно: повтор той же транзакции не начисляет повторно.

### Response (200)
```json
{
  "creditsAdded": 1500,
  "newBalance": 2730,
  "transactionId": "string"
}
```
- При повторной (уже обработанной) транзакции: `creditsAdded=0`, `newBalance` = текущий (идемпотентный ответ).

## GET /v1/tokens/products (опц.)
Каталог пакетов токенов (маппинг credits; цены — из StoreKit на клиенте).
### Response (200)
```json
{
  "products": [
    { "productId": "tokens_1500", "credits": 1500 },
    { "productId": "tokens_600",  "credits": 600 },
    { "productId": "tokens_250",  "credits": 250 },
    { "productId": "tokens_100",  "credits": 100 }
  ]
}
```
- Источник — `TOKEN_PRODUCTS` (env/config, [07-deployment.md](../../07-deployment.md)).

## Ошибки
- **Нет активной подписки → `403` `{code: "subscription_required"}`** ([Q-015-1](../../99-open-questions.md) вариант B; код консистентен с enum [ADR-004](../../adr/ADR-004-blocked-http-200.md), здесь — как `code` в error-теле `4xx`, не `blockReason`+`200`, т.к. это не endpoint генерации).
- Неизвестный `productId` → `422`. Невалидная транзакция → `422`/`400`. `userId` ≠ `sub` → `403` (`code=forbidden`).
- `401` (нет/невалидный JWT), `429` (rate limit), `5xx` (App Store API / внутренняя ошибка).
