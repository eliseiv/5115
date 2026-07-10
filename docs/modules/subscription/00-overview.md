# Subscription — Overview

## Scope
- `POST /v1/subscription/sync` — приём StoreKit transaction payload, server-side верификация (App Store Server API / JWS-валидация подписанной транзакции), обновление `subscriptions`.
- Нормализация статуса: active / expired / none на основе `expiresAt` и состояния транзакции.
- Grant кредитов при активации/продлении плана (вызов Wallet.grant, фикс. пакет на период), [ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md).
- Обработка refund/revocation → перевод в expired ([Q-007-1](../../99-open-questions.md)).

## Out of scope
- Решение о доступе (Policy Engine читает subscription).
- Покупка/выставление счетов (выполняет Apple/StoreKit на клиенте).

## Безопасность
- Верификация только server-side; нельзя доверять клиентскому `isSubscribed`.
- StoreKit payload не логируется целиком (redaction).
