# Admin — API Contracts

Все admin-эндпоинты под префиксом `/v1/admin/*`. Авторизация — заголовок `X-Admin-Token` (изолированный
admin-секрет, [ADR-009](../../adr/ADR-009-admin-token-auth.md)), зависимость `require_admin`. **Пользовательский
JWT не авторизует admin-действия.** Отсутствие/несовпадение токена → `401`. Отдельный rate limit (дефолт 10 req/min
per source IP, конфигурируемо), `extra='forbid'`, тело ≤ 8 KB.

## POST /v1/admin/wallet/grant
Начисление кредитов пользователю (саппорт/компенсация).

### Headers
- `X-Admin-Token: <ADMIN_API_SECRET>` (обязателен).

### Request
```json
{
  "userId": "uuid",
  "amount": 100,
  "idempotencyKey": "string",
  "reason": "string"
}
```
- `userId` — UUID существующего пользователя (см. Правила §Несуществующий userId).
- `amount` — целое **> 0** (BIGINT, целые кредиты, без дробей). `amount <= 0` → `422`.
- `idempotencyKey` — непустая строка, `max_length` 128. Ключ идемпотентности начисления (передаётся в `WalletService.grant(idempotency_key=...)`).
- `reason` — **обязателен**, непустая строка, `max_length` 512. Пишется в audit `admin_grant` и `ledger_transactions.meta`.

### Response (200)
```json
{
  "newBalance": 1100,
  "ledgerTxId": "uuid",
  "idempotentReplay": false
}
```
- `newBalance` — баланс после начисления.
- `ledgerTxId` — id `ledger_transactions` (`type=credit`).
- `idempotentReplay` — `true`, если ключ уже был использован с тем же payload (повторного начисления не было).

### Правила
- Переиспользует `WalletService.grant(user_id, amount, idempotency_key, meta, reason)` (`src/app/wallet/service.py:174`)
  — атомарно, идемпотентно по `(user_id, idempotency_key)`, пишет `ledger_transactions(type=credit)` + audit `billing_credit`.
- **Дополнительно** пишется audit-событие `admin_grant` (actor=admin, `userId`, `amount`, `reason`, `idempotencyKey`,
  `ledgerTxId`) — отдельно от `billing_credit`, фиксирует именно admin-инициацию. **Секрет `X-Admin-Token` в audit не пишется.**
- Идемпотентность: тот же `idempotencyKey` + тот же payload → тот же `ledgerTxId`, `idempotentReplay=true`, без повторного начисления.
- Тот же `idempotencyKey`, **другой** `amount` → `409` (конфликт, как в `WalletService.grant`), без начисления.
- **Несуществующий userId → `404 {error.code:"user_not_found"}`** (admin-grant **не создаёт** пользователей — см. 03-architecture; обоснование ниже).
- `reason` отсутствует/пустой → `422`.

## POST /v1/admin/subscription/grant
Ручная активация/продление подписки пользователю (саппорт/компенсация/тестирование) — **без** StoreKit-транзакции ([ADR-048](../../adr/ADR-048-admin-subscription-grant.md)).
Зачем отдельно от `wallet/grant`: по [ADR-002](../../adr/ADR-002-access-policy-state-machine.md) при `subscription_status=none` кредиты **не проверяются** — пользователь блокируется по `trial_used`
**даже с ненулевым балансом**, поэтому одного начисления кредитов недостаточно, нужна активная подписка.

### Headers
- `X-Admin-Token: <ADMIN_API_SECRET>` (обязателен).

### Request
```json
{
  "userId": "uuid",
  "expiresAt": "2026-12-31T23:59:59Z",
  "days": 30,
  "plan": "manual_grant",
  "idempotencyKey": "string",
  "credits": 1000
}
```
- `userId` — UUID существующего пользователя. Отсутствует → `404 user_not_found` (admin **не создаёт** пользователей, [ADR-007](../../adr/ADR-007-lazy-user-provisioning.md)).
- Срок — **ровно одно** из `expiresAt` / `days` (оба или ни одного → `422`):
  - `expiresAt` — ISO8601 datetime, **tz-aware** и **строго в будущем** (`> now()`). В прошлом/naive → `422`. Требование «в будущем» обязательно: policy-loader (`src/app/policy/loader.py`) применяет lazy-expiry — `active` c `expires_at <= now()` трактуется как `expired`, т.е. грант в прошлое не дал бы доступа.
  - `days` — положительный int (`> 0`); сервер вычисляет `expires_at = now() + days`. `≤ 0` → `422`.
- `plan` — опц. строка (`max_length` 128), метка плана. Дефолт `"manual_grant"`.
- `idempotencyKey` — **обязателен**, непустая строка (`max_length` 128). Ключ идемпотентности начисления кредитов.
- `credits` — опц. int `≥ 0`. **Опущено (null) → `SUBSCRIPTION_CREDITS_PER_PERIOD`** (тот же пакет, что даёт реальный период — активированная подписка сразу рабочая). Явный `0` → активировать подписку **без** начисления (у пользователя уже есть баланс). `< 0` → `422`.

### Response (200)
```json
{
  "status": "active",
  "expiresAt": "2026-12-31T23:59:59Z",
  "plan": "manual_grant",
  "creditsGranted": 1000,
  "newBalance": 1100,
  "ledgerTxId": "uuid",
  "idempotentReplay": false
}
```
- `status` — новый статус подписки (`"active"`).
- `expiresAt` — эффективный момент истечения (из `expiresAt` или `now()+days`), ISO8601 | `null`.
- `plan` — записанный план | `null`.
- `creditsGranted` — эффективно начисленная сумма (0, если не начислялось).
- `newBalance` / `ledgerTxId` / `idempotentReplay` — присутствуют (не `null`) **только** при `creditsGranted > 0`; иначе `null` (ledger-транзакции нет).

### Правила
- Upsert строки `subscriptions` (PK `user_id`): `status='active'`, `plan`, `expires_at`. Прямая запись через ORM `Subscription`, **без** StoreKit-верификации (в отличие от `/v1/subscription/sync`). Idempotent по PK: повтор перезаписывает те же значения.
- При эффективной сумме `> 0` — начисление через `WalletService.grant(...)` **как есть** (`src/app/wallet/service.py:174`): атомарно, идемпотентно по `(user_id, idempotency_key)`, ledger `credit` + audit `billing_credit`. Ledger-ключ **производный с namespace**: `admin-sub-grant:{idempotencyKey}` (не коллидирует с `admin/wallet/grant` и `sub-grant:{transaction_id}`).
- Тот же `idempotencyKey` c **другим** `credits` → `409` (из `WalletService.grant`), активации/начисления нет.
- **Дополнительно** пишется audit-событие `admin_subscription_grant` (actor=admin, `userId`, `plan`, `status`, `expiresAt`, `creditsGranted`, `idempotencyKey`, `ledgerTxId` при наличии). **Секрет `X-Admin-Token` не логируется/не в audit.**
- Всё (upsert + grant + оба audit) — в **одной** транзакции запроса.
- **Коды:** `200`; `401`; `404` (`user_not_found`); `409` (тот же `idempotencyKey`, другой `credits`); `422` (нет `userId` / оба|ни одного из `expiresAt`/`days` / `expiresAt` не tz-aware|в прошлом / `days ≤ 0` / `credits < 0` / схема); `429`; `5xx`.
- **OpenAPI-тексты** (`summary`, `description` роута, `Field(description=...)`, тег `Admin`) — по [08-api-documentation §R2ter](../../08-api-documentation.md#r2ter-лаконичность-user-facing-текстов-для-тестировщиков): лаконичные профессиональные формулировки для оператора, **без** ссылок `ADR-`/`Q-`/`TD-` и расшифровок-аббревиатур в скобках. Внутренняя мотивация (root-cause ADR-002, обоснование дефолтов, namespace-ключ) — только здесь и в [ADR-048](../../adr/ADR-048-admin-subscription-grant.md), **не** в OpenAPI-строках.

## GET /v1/admin/wallet/{userId}
Read-only просмотр кошелька для саппорта.

### Headers
- `X-Admin-Token: <ADMIN_API_SECRET>` (обязателен).

### Response (200)
```json
{
  "userId": "uuid",
  "balance": 1100,
  "lastTransactions": [
    { "id": "uuid", "type": "credit|debit", "amount": 100, "createdAt": "ISO8601", "meta": {} }
  ]
}
```
- Переиспользует `WalletService.get_wallet_view(user_id, last_n)` (дефолт `last_n=20`, по `created_at DESC`).
- `meta` — без секретов (usage/model/reason).

### Правила
- Несуществующий `userId` → `404 {error.code:"user_not_found"}` (read-only не создаёт пользователя).
- Только чтение; не мутирует состояние и не пишет мутирующий audit (логируется на уровне tool/request lifecycle).

## Обоснование «404 на несуществующем userId» (не admin-provisioning)
Admin-grant **не создаёт** пользователей. Причины:
- Источник истины идентичности — доверенный JWT issuer ([ADR-007](../../adr/ADR-007-lazy-user-provisioning.md));
  создание `users` из admin-API ввело бы второй, неаутентифицированный путь рождения идентичности и риск
  начисления на «фантомный» (опечатанный) `userId`.
- `404` делает опечатку в `userId` видимой оператору сразу, вместо молчаливого создания мусорного аккаунта с балансом.
- Реальные пользователи создаются лениво при первом аутентифицированном запросе (ADR-007); к моменту легитимного
  admin-grant пользователь, как правило, уже существует. Если нужно начислить «наперёд» — это отдельный продуктовый
  вопрос, не решается тихим созданием строки. См. [Q-009-2](../../99-open-questions.md) (не блокер; дефолт — `404`).
