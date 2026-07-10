# billing-cloudpayments / 01 — Context

## Соседи и зависимости

| Зависимость | Что используется | Источник |
|---|---|---|
| Auth-образец | constant-time bearer (`hmac.compare_digest`), `auto_error=False` security scheme | `src/app/billing_adapty/auth.py`, `src/app/api_gateway/openapi_security.py` (образец `adapty_webhook_scheme`) |
| Wallet | `WalletService.grant(*, user_id, amount, idempotency_key, meta, reason) -> GrantResult` — идемпотентный кредит-грант | `src/app/wallet/service.py` ([ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md)) |
| Subscription upsert | `INSERT subscriptions ... ON CONFLICT (user_id) DO UPDATE` (status/plan/expires_at) | образец `src/app/admin/service.py::grant_subscription` ([ADR-048](../../adr/ADR-048-admin-subscription-grant.md)) |
| Config | `token_products()` (JSON-парсинг env-карты) — образец для `cloudpayments_product_tokens()` | `src/app/config.py:293` |
| Audit | `AuditService.record`, `assert_no_secrets` | `src/app/audit/service.py`, `src/app/observability/redaction.py` |
| Observability | `log_event(logger, level, msg, **fields)` | образец `app.billing_adapty.service` ([ADR-046](../../adr/ADR-046-adapty-webhook-outcome-logging.md)) |
| Policy | читает `subscriptions.status` (active/expired) | [ADR-002](../../adr/ADR-002-access-policy-state-machine.md) |
| Router registration | `app.include_router(...)`, глобального auth-middleware нет | `src/app/main.py` |

## Кто вызывает
- **Входящий вебхук** (`/webhook`): **broadapps (внешний агрегатор)** — серверный HTTP POST в формате CloudPayments (фронтит YooKassa). Не наш iOS-клиент. **[ADR-054](../../adr/ADR-054-cloudpayments-webhook-payment-verification.md): колбэк приходит БЕЗ авторизации/подписи → эндпоинт публичный (нет `401`), rate-limit per-IP.** Аутентичность события устанавливается **верификацией** через broadapps API (`GET /users/{deviceId}/payments`, `Bearer CLOUDPAYMENTS_API_TOKEN`), а не токеном колбэка. `CLOUDPAYMENTS_WEBHOOK_TOKEN` — легаси/опционален.
- **Исходящий checkout** (`/checkout`, [ADR-051](../../adr/ADR-051-cloudpayments-checkout-payment-link.md)): **наш iOS-клиент** (JWT). Мы, в свою очередь, **вызываем broadapps** `POST /payments/link` (исходящий httpx, `Authorization: Bearer <CLOUDPAYMENTS_API_TOKEN>`).

## Исходящая зависимость (checkout)
| Зависимость | Что используется | Источник |
|---|---|---|
| httpx-клиент к внешнему API | `httpx.AsyncClient` POST multipart, таймаут, маппинг ошибок → `UpstreamError` (502) | образец исходящих клиентов `src/app/chat/openai_client.py` ([ADR-033](../../adr/ADR-033-llm-provider-abstraction.md)) |
| Валидация продукта | `parser.classify_product` (переиспользуется как allowlist-предикат) | `src/app/billing_cloudpayments/parser.py` ([ADR-050](../../adr/ADR-050-cloudpayments-webhook.md)) |
| JWT / провижининг | `CurrentUser` → `user_id=sub`, ленивый provision `users` | `src/app/deps.py` ([ADR-007](../../adr/ADR-007-lazy-user-provisioning.md)) |
| Rate-limit | `enforce_other_limits(user_id)` | `src/app/api_gateway/rate_limit.py` |

## Соотношение с существующими путями биллинга
- `POST /v1/billing/adapty/webhook` (модуль [billing-adapty](../billing-adapty/README.md)) — Adapty (Apple), **остаётся**, не пересекается.
- `POST /v1/subscription/sync` (модуль [subscription](../subscription/README.md)) — StoreKit JWS, **остаётся**.
- `POST /v1/tokens/purchase` (модуль [token-purchase](../token-purchase/README.md)) — consumable IAP, **остаётся**. CloudPayments-путь **переиспользует** его карту `TOKEN_PRODUCTS` для token-пакетов.

> **Инвариант:** один путь платежей на `userId`/инстанс (см. [00-overview.md](00-overview.md)). Разные ledger-namespace'ы (`cp-txn:*` vs `sub-grant:*`/`adapty-txn:*`) не защищают между путями.

## Данные
- Использует существующие таблицы `users` (lookup по нормализованному `AccountId`=UUID), `subscriptions` (upsert), `wallets`, `ledger_transactions`.
- Вводит новую таблицу `cloudpayments_webhook_events` (см. [04-data-model.md](04-data-model.md), миграция `0014`).

## Причина появления (инцидент)
broadapps был направлен на Adapty-эндпоинт → `401` (несовпадение `ADAPTY_WEBHOOK_SECRET`) + несовместимый формат (Adapty ждёт `profile_event_id`/`event_properties`). Нужен свой эндпоинт + секрет + парсер CloudPayments-формата ([ADR-050 §Context](../../adr/ADR-050-cloudpayments-webhook.md)).
