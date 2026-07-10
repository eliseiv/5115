# billing-cloudpayments / 04 — Data Model

## Таблица: `cloudpayments_webhook_events` (миграция `0014`; семантика значений пересмотрена [ADR-054](../../adr/ADR-054-cloudpayments-webhook-payment-verification.md))

Журнал начисленных платежей broadapps. Единая точка дедупликации + аудиторский след **санитизированного** payload (без карт-данных). Миграция **`0014`** (`down_revision="0013"`, single head, expand-only) — **уже существует; [ADR-054](../../adr/ADR-054-cloudpayments-webhook-payment-verification.md) НЕ добавляет миграцию** (репурпозинг значений колонок без DDL; прод-таблица пуста от успешных строк).

> **[ADR-054](../../adr/ADR-054-cloudpayments-webhook-payment-verification.md) — репурпозинг колонок (без изменения схемы):** единица дедупа/идемпотентности — теперь **broadapps `payment_id`** (стабильный id платежа из `GET /users/{deviceId}/payments`), НЕ callback `TransactionId`. Колонка `transaction_id` **хранит `payment_id`**; `user_id` — **резолвнутый** наш `userId` (после двухступенчатого резолва deviceId→userId, [ADR-053](../../adr/ADR-053-cloudpayments-webhook-user-resolution-via-auth-devices.md)); `product_id` — `product.code` из верифицированного ответа. Переименование колонки `transaction_id`→`payment_id` — отложенный не-блокирующий долг ([Q-054-3](../../99-open-questions.md)).

| Колонка | Тип | Ограничения | Назначение ([ADR-054](../../adr/ADR-054-cloudpayments-webhook-payment-verification.md)) |
|---|---|---|---|
| `transaction_id` | `text` | **PRIMARY KEY** (= UNIQUE) | **broadapps `payment_id`** (репурпозинг; ранее callback `TransactionId`) — точка дедупа/идемпотентности на **платёж** |
| `user_id` | `uuid` | `NOT NULL`, FK `users(id) ON DELETE CASCADE` | **резолвнутый** `userId` (deviceId→userId через `auth_devices`, [ADR-053](../../adr/ADR-053-cloudpayments-webhook-user-resolution-via-auth-devices.md)), НЕ исходный `AccountId`/deviceId |
| `product_id` | `text` | `NOT NULL` | **`product.code`** из верифицированного ответа broadapps (ранее `Data.product_id`) |
| `kind` | `text` | `NOT NULL` | класс по `product.payment_type`: `subscription` (`payment_type=subscription`) \| `tokens` (`payment_type=one_time`) |
| `payload` | `jsonb` | `NOT NULL` | **санитизированная** проекция события (без PII карт/токена) |
| `processed_at` | `timestamptz` | `NOT NULL DEFAULT now()` | момент обработки |

### DDL (ориентир для миграции 0014)
```sql
CREATE TABLE cloudpayments_webhook_events (
    transaction_id text        PRIMARY KEY,
    user_id        uuid        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    product_id     text        NOT NULL,
    kind           text        NOT NULL,
    payload        jsonb       NOT NULL,
    processed_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_cloudpayments_webhook_events_user_id ON cloudpayments_webhook_events (user_id);
```

### Санитизированный `payload` (allowlist — что МОЖНО хранить)
**[ADR-054](../../adr/ADR-054-cloudpayments-webhook-payment-verification.md) (из верифицированного платежа):** `paymentId`, `productCode`, `paymentType`, `kind`, `status`, `paidAt`, `subscriptionId`; опц. контекст колбэка `transactionId` (callback, для корреляции). (`amount`/`currency` из ответа verify — **не** источник суммы; хранить не обязательно, при хранении — только для аудита, не для начисления.)

**ЗАПРЕЩЕНО** в `payload` (PII/секреты): `CardFirstSix`, `CardLastFour`, `Issuer`, `CardType`, `Authorization`/bearer, `CLOUDPAYMENTS_API_TOKEN`, `Data`-строка целиком, тело ответа verify целиком, любые прочие карт-поля. Отличие от `adapty_webhook_events` (там полный parsed-объект — карт-данных нет; здесь — только явный allowlist).

### Замечания
- `transaction_id` (=broadapps `payment_id`, [ADR-054](../../adr/ADR-054-cloudpayments-webhook-payment-verification.md)) как PK даёт UNIQUE-гарантию и `ON CONFLICT (transaction_id) DO NOTHING` для дедупа **на платёж** (см. [03-architecture.md §Реконсиляция](03-architecture.md)).
- Строка пишется **на каждый начисленный платёж** (реконсиляция может начислить несколько платежей за один колбэк); повторный/дубль-платёж (тот же `payment_id`) → `ON CONFLICT DO NOTHING` → пропуск. `ignored`/пропущенные (`unknown_product`/`unknown_payment_type`) строк не создают.
- Index по `user_id` — для будущих выборок «платежи пользователя» (диагностика). В горячем пути запросов по нему нет.

## Затронутые существующие таблицы (без изменения схемы)
- `subscriptions` — upsert по `user_id` (`ON CONFLICT (user_id) DO UPDATE`, образец [ADR-048](../../adr/ADR-048-admin-subscription-grant.md)) для класса `subscription` (`status='active'`, `plan=product_id`, `expires_at`). Для `tokens` — **не трогается**. Схема: `src/app/models/tables.py`, enum `subscription_status ∈ none|active|expired`.
- `ledger_transactions` — грант кредитов идемпотентно по `(user_id, idempotency_key="cp-txn:{payment_id}")` (broadapps `payment_id`, [ADR-054](../../adr/ADR-054-cloudpayments-webhook-payment-verification.md); ранее `TransactionId`, [ADR-005](../../adr/ADR-005-idempotency-ledger.md)). Один грант на платёж. Namespace `cp-txn:*` изолирован. Схема не меняется.
- `wallets` — баланс инкрементируется внутри `WalletService.grant`.
- `users` — lookup по id (нормализованный `AccountId`); **не создаётся** вебхуком.

## ORM
Добавить модель `CloudPaymentsWebhookEvent` в `src/app/models/tables.py`. Без новых enum-типов.

## Миграция 0014 (инварианты)
- `down_revision = "0013"` (после `byok_provider`); **single head** — проверить `alembic heads` = один.
- **Expand-only** (только CREATE TABLE + CREATE INDEX), без backfill, без изменения существующих таблиц.
- `upgrade` создаёт таблицу+индекс; `downgrade` — `DROP TABLE cloudpayments_webhook_events`.
