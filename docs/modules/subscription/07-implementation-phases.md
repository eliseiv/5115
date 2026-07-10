# Subscription — Implementation Phases

| Phase | Задача | Зависит от |
|---|---|---|
| SB-1 | Модель + миграция subscriptions. | DB |
| SB-2 | StoreKit verification (JWS / App Store Server API client, httpx). | SB-1, Q-007-1 (дефолт) |
| SB-3 | `/v1/subscription/sync`: verify → normalize → upsert → response. | SB-2 |
| SB-4 | Grant при активации/продлении (Wallet.grant фикс. пакета `SUBSCRIPTION_CREDITS_PER_PERIOD`, дефолт 1000; идемпотентно по transactionId периода). [ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md). | SB-3, Wallet |
| SB-5 | refund/revocation handling → expired. | SB-3 |
| SB-6 | audit subscription_change. | SB-3, Audit |

> Q-006-1 закрыт (ADR-006): SB-4 разблокирован. Начисление — фикс. пакет на период, идемпотентно по transactionId.
