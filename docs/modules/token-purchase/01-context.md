# Token Purchase — Context

## Зависимости
- **API Gateway** — auth, provisioning, роут `/v1/tokens/*`.
- **subscription** (verifier) — переиспользует StoreKit-верификатор (реальная JWS / App Store Server API, fail-closed; `STOREKIT_TEST_MODE` для e2e/CI, [TD-007](../../100-known-tech-debt.md)). Verifier выделен как общий компонент, не дублируется.
- **wallet-ledger** — `Wallet.grant(credits, idempotency_key=transactionId, type=credit, meta={source:token_purchase, productId})`. Единственный, кто пишет в ledger (инвариант сохранён).

## Разграничение с subscription ([ADR-015](../../adr/ADR-015-consumable-token-iap.md))
- `subscription/sync` → grant фикс. пакета на период (idempotency = transactionId периода), ADR-006 — без изменений.
- `tokens/purchase` → grant по consumable transactionId. Разные источники StoreKit, разные idempotency-значения; `meta.source` различает.

## Границы
- Token-purchase — тонкая обёртка: verify → map → grant. Не вызывает Anthropic, не меняет policy-логику, не трогает subscription-статус.
