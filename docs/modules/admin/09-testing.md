# Admin — Testing

## Unit
- `require_admin`: валидный `X-Admin-Token` → проходит; неверный/отсутствует → `401`; сравнение constant-time (по контракту,
  не таймингом). Совпадение с `ADMIN_API_SECRET_PREV` (ротация) → проходит. Пустые секреты в конфиге не матчатся.
- Pydantic-схема grant: `amount<=0` → `422`; пустой/отсутствующий `reason` → `422`; лишнее поле (`extra='forbid'`) → `422`.

## Integration (реальный PostgreSQL)
- `grant` на существующем `userId` → `ledger_transactions(type=credit)`, баланс += amount, `idempotentReplay=false`.
- Повторный `grant` тот же `idempotencyKey` + payload → `idempotentReplay=true`, баланс не меняется, тот же `ledgerTxId`.
- Тот же `idempotencyKey`, другой `amount` → `409`, без начисления.
- Несуществующий `userId` → `404 user_not_found`, строка `users` **не** создана, баланс не появился.
- `require_admin` не создаёт `users` для actor (нет provisioning): после серии admin-запросов нет «admin»-строки в `users`.
- `users.trial_used` не изменяется admin-операциями.
- audit: успешный `grant` создаёт **и** `billing_credit` (Wallet), **и** `admin_grant` (Admin). Секрет в payload отсутствует.
- `GET /v1/admin/wallet/{userId}`: корректные `balance` + `lastTransactions` (DESC); несуществующий → `404`.

## Integration — subscription/grant ([ADR-048](../../adr/ADR-048-admin-subscription-grant.md))
- `POST /v1/admin/subscription/grant` на существующем `userId` (нет строки `subscriptions`) → **создаёт** строку `status='active'`, `plan`, `expires_at`; ответ `status='active'`, `expiresAt`, `plan`.
- Повторный вызов на существующей подписке (upsert) **идемпотентен по PK `user_id`**: перезаписывает те же значения, второй строки не появляется.
- `credits` **опущен** → начислено `SUBSCRIPTION_CREDITS_PER_PERIOD` (default); ответ `creditsGranted=SUBSCRIPTION_CREDITS_PER_PERIOD`, `newBalance`/`ledgerTxId`/`idempotentReplay` присутствуют.
- `credits=0` → активация **без** начисления: `ledger_transactions` не растёт, `creditsGranted=0`, `newBalance`/`ledgerTxId`/`idempotentReplay` = `null`.
- `credits=N` (N>0) → начислено ровно N; `creditsGranted=N`.
- **Namespace ledger-ключа:** начисление идёт по `admin-sub-grant:{idempotencyKey}` и **НЕ коллидирует** с `wallet/grant` (raw `idempotencyKey`), с `sub-grant:{transaction_id}` (реальный период) и `adapty-txn:{...}` (Adapty): один и тот же человекочитаемый `idempotencyKey`, использованный на `wallet/grant` и на `subscription/grant`, порождает **две разные** ledger-транзакции.
- Повтор `subscription/grant` с тем же `idempotencyKey` + тот же `credits` → `idempotentReplay=true`, баланс не меняется, тот же `ledgerTxId`.
- Тот же `idempotencyKey`, **другой** `credits` → `409` (из `WalletService.grant`); **ни** подписка **не** активирована повторно с иными полями сверх upsert, **ни** кредиты не начислены (одна транзакция откатывается целиком).
- Несуществующий `userId` → `404 user_not_found`, строка `users` **не** создана, `subscriptions` не появилась, баланс не появился (нет provisioning).
- **Одна транзакция (нет частичного применения):** при сбое начисления (`409`/insufficient) upsert подписки **не** коммитится — состояние `subscriptions`/`wallets`/`ledger` не меняется.

## Policy / E2E — root-cause ([ADR-002](../../adr/ADR-002-access-policy-state-machine.md))
- Пользователь с `subscription_status=none` и `trial_used=true`, баланс `>0`: `/v1/chat/run` (или `/v1/policy/effective`) → **blocked** (`trial_used`) **до** гранта.
- После `POST /v1/admin/subscription/grant` (expiresAt в будущем, `credits` по умолчанию): тот же пользователь **проходит** policy (`allow`) — сняты `trial_used` и `credits_empty`; подписка `active` + баланс `>0`.
- **lazy-expiry:** грант с `expiresAt <= now()` был бы отклонён валидацией (`422`), поэтому кейс «active с прошлой датой → всё равно expired» не достижим через endpoint (регресс-защита `_effective_subscription_status`).

## Validation — subscription/grant
- `expiresAt` в прошлом → `422`; `expiresAt` без tzinfo (naive) → `422`.
- `days <= 0` → `422`.
- Заданы **оба** `expiresAt` и `days` → `422`; **ни одного** → `422`.
- `credits < 0` → `422`; лишнее поле (`extra='forbid'`) → `422`; отсутствует `userId`/`idempotencyKey` → `422`.

## Security
- Пользовательский JWT на `/v1/admin/*` (без `X-Admin-Token`) → `401` (JWT не авторизует admin).
- `X-Admin-Token` на пользовательском роуте (`/v1/wallet`) не даёт доступа (там нужен JWT).
- `X-Admin-Token` не попадает в логи/audit (redaction).
- Rate limit `/v1/admin/*`: превышение дефолта → `429`, изолировано от пользовательских лимитов.
- Size-лимит admin-grant: тело > 8 KB → `413` (действует и на `subscription/grant`).
- audit: успешный `subscription/grant` создаёт `admin_subscription_grant` (actor=admin, `plan`/`status`/`expiresAt`/`creditsGranted`), при начислении — **и** `billing_credit`. Секрет `X-Admin-Token` в payload **отсутствует**.
