# billing-adapty / 01 — Context

## Соседи и зависимости

| Зависимость | Что используется | Источник |
|---|---|---|
| Auth-образец | constant-time bearer (`hmac.compare_digest`), `auto_error=False` security scheme | `src/app/api_gateway/auth.py:99-134`, `src/app/api_gateway/openapi_security.py` |
| Wallet | `WalletService.grant(*, user_id, amount, idempotency_key, meta, reason) -> GrantResult` — идемпотентный кредит-грант | `src/app/wallet/service.py:174-236` ([ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md)) |
| Subscription | upsert строки `subscriptions` (status/plan/expires_at) | `src/app/subscription/service.py:52-68` |
| Config | образец JSON-парсинга env-карты `token_products()` | `src/app/config.py:199` |
| Audit | `AuditService.record`, `assert_no_secrets` | `src/app/audit/service.py:48`, `src/app/observability/redaction.py` |
| Policy | читает `subscriptions.status` (active/expired) | [ADR-002](../../adr/ADR-002-access-policy-state-machine.md) |
| Router registration | `app.include_router(...)`, глобального auth-middleware нет | `src/app/main.py:196-212` |

## Кто вызывает
- **Adapty (внешний сервис)** — серверный HTTP POST. Не наш iOS-клиент. Аутентификация — статический bearer-секрет, заданный оператором в Adapty UI.

## Соотношение с существующими путями биллинга
- `POST /v1/subscription/sync` (модуль [subscription](../subscription/README.md)) — StoreKit JWS, **остаётся**. Источник истины по подпискам сместился на Adapty.
- `POST /v1/tokens/purchase` (модуль [token-purchase](../token-purchase/README.md)) — consumable IAP, **остаётся**, не через Adapty.

> **Инвариант:** один путь подписок на клиенте (см. [00-overview.md](00-overview.md)). Двойной путь = двойное начисление (разные idempotency-ключи).

## Данные
- Использует существующие таблицы `users` (lookup по `customer_user_id`=UUID), `subscriptions`, `wallets`, `ledger_transactions`.
- Вводит новую таблицу `adapty_webhook_events` (см. [04-data-model.md](04-data-model.md), миграция `0008`).
