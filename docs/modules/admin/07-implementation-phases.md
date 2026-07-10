# Admin — Implementation Phases

| Phase | Задача | Зависит от |
|---|---|---|
| ADM-1 | Config: `ADMIN_API_SECRET` (+ опц. `ADMIN_API_SECRET_PREV`), `ADMIN_RATE_LIMIT_PER_MIN` (дефолт 10) в pydantic-settings. Добавить `X-Admin-Token` в redaction allowlist. | — |
| ADM-2 | Зависимость `require_admin` (constant-time compare обоих секретов, `401` при несовпадении; **без** provisioning/trial/`get_current_user`). | ADM-1 |
| ADM-3 | Роутер `api_gateway/routers/admin.py` (`/v1/admin/*`) + отдельный rate limit per source IP + size-лимит ≤ 8 KB. | ADM-2 |
| ADM-4 | `POST /v1/admin/wallet/grant`: Pydantic-схема (`extra='forbid'`, `amount>0`, `reason` непустой); проверка существования `users(userId)` → `404`; вызов `WalletService.grant`; audit `admin_grant`; ответ `{newBalance, ledgerTxId, idempotentReplay}`. | ADM-3, Wallet |
| ADM-5 | `GET /v1/admin/wallet/{userId}`: проверка существования → `404`; `WalletService.get_wallet_view`. | ADM-3, Wallet |
| ADM-6 | Audit: новый `eventType=admin_grant` в каталоге Audit; убедиться, что секрет не логируется. | ADM-4, Audit |
| ADM-7 | Метрика `admin_grant_total{result=success|conflict|not_found}` (observability). | ADM-4 |
| ADM-8 | `POST /v1/admin/subscription/grant` ([ADR-048](../../adr/ADR-048-admin-subscription-grant.md)): Pydantic-схема `AdminSubscriptionGrantRequest` (`extra='forbid'`; **XOR-валидатор** «ровно одно из `expiresAt`/`days`»; `expiresAt` — tz-aware **и** строго `> now()`; `days>0`; `credits≥0`; `plan` дефолт `manual_grant`); `AdminService.grant_subscription` — проверка `users(userId)` → `404`, **ORM-upsert `Subscription`** (`status='active'`, `plan`, `expires_at`) без StoreKit-verify, опц. `WalletService.grant` с **namespace-ключом** `admin-sub-grant:{idempotencyKey}` (default `credits`=`SUBSCRIPTION_CREDITS_PER_PERIOD`, `credits=0`→без начисления, `409` на тот же ключ+другой `credits`); audit `admin_subscription_grant` (actor=admin, без секрета); всё в одной транзакции; ответ `{status, expiresAt, plan, creditsGranted, newBalance?, ledgerTxId?, idempotentReplay?}`. Новый `eventType=admin_subscription_grant` в каталоге Audit. Опц. метрика `admin_subscription_grant_total{result=success|conflict|not_found}`. Без миграции. | ADM-2, ADM-3, Wallet |

> Admin-модуль не дублирует биллинг — тонкая обёртка над существующим `WalletService.grant`/`get_wallet_view`
> ([ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md), [ADR-009](../../adr/ADR-009-admin-token-auth.md)).
> ADM-8 не дублирует подписочную логику `SubscriptionService` — прямой verify-less upsert `Subscription` в `AdminService`
> ([ADR-048](../../adr/ADR-048-admin-subscription-grant.md); обоснование изоляции verify-less пути — [Q-048-2](../../99-open-questions.md)).
