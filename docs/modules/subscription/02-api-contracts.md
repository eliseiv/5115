# Subscription — API Contracts

## POST /v1/subscription/sync
### Request
```json
{
  "userId": "uuid",
  "transaction": { "...StoreKit transaction payload (signed)..." }
}
```
- `transaction` — подписанный StoreKit payload (JWS signed transaction / App Store receipt). Конкретный формат — App Store Server API.

### Response (200)
```json
{
  "isSubscribed": true,
  "expiresAt": "ISO8601 | null",
  "plan": "string | null"
}
```

### Правила
- Сервер **верифицирует** транзакцию (подпись/через App Store Server API), не доверяет клиенту.
- Идемпотентность: повторный sync той же транзакции не создаёт дублирующих grant (по transactionId в meta).
- При успешной активации/продлении нового периода → Wallet.grant фикс. пакета `SUBSCRIPTION_CREDITS_PER_PERIOD` (дефолт 1000) кредитов, идемпотентно по `transactionId` периода ([ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md)).
- **Разовая покупка пакетов токенов** (consumable IAP) — **отдельный** endpoint `POST /v1/tokens/purchase` (модуль [token-purchase](../token-purchase/README.md), [ADR-015](../../adr/ADR-015-consumable-token-iap.md)), НЕ через `subscription/sync`. Использует общий StoreKit verifier, но отдельный путь grant (idempotency по consumable `transactionId`, `meta.source=token_purchase`). Subscription grant этим не затрагивается.
- refund/revocation → `status=expired`, `isSubscribed=false`.
- Невалидная/поддельная транзакция → `422`/`400` (тех. ошибка), подписка не меняется.
- StoreKit payload не логируется (redaction, [05-security.md](../../05-security.md)).
- **Test-mode (только e2e/CI, `STOREKIT_TEST_MODE=true`):** `transaction` принимается как HS256-JWS,
  подписанный `STOREKIT_TEST_SECRET`; извлекаются те же поля (`transactionId`/`expiresDate`/`productId`/
  …), активация и grant идут штатно. В prod (`STOREKIT_TEST_MODE=false`, дефолт) принимаются только
  реальные Apple-подписанные транзакции. См. [03-architecture.md](03-architecture.md#test-mode-верификации-storekit_test_mode), [TD-007](../../100-known-tech-debt.md).
