# billing-adapty / 04 — Data Model

## Новая таблица: `adapty_webhook_events`

Журнал обработанных вебхук-событий Adapty. Единая точка дедупликации (UNIQUE `event_id`) и аудиторский след сырого payload. Миграция **`0008`** (линейная цепочка, после `0007`).

| Колонка | Тип | Ограничения | Назначение |
|---|---|---|---|
| `event_id` | `text` | **PRIMARY KEY** (= UNIQUE) | внешний идентификатор события Adapty — **значение `profile_event_id`** (ADR-047, не `event_id`/`id`); точка дедупа **доставки события** |
| `user_id` | `uuid` | `NOT NULL`, FK `users(id) ON DELETE CASCADE` | целевой пользователь (`customer_user_id`) |
| `event_type` | `text` | `NOT NULL` | нормализованный (`lower`) тип события |
| `payload` | `jsonb` | `NOT NULL` | распарсенный объект события (для аудита/диагностики) |
| `processed_at` | `timestamptz` | `NOT NULL DEFAULT now()` | момент обработки |

### DDL (ориентир для миграции 0008)
```sql
CREATE TABLE adapty_webhook_events (
    event_id     text        PRIMARY KEY,
    user_id      uuid        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    event_type   text        NOT NULL,
    payload      jsonb       NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_adapty_webhook_events_user_id ON adapty_webhook_events (user_id);
```

### Замечания
- `event_id` как PK даёт UNIQUE-гарантию и `ON CONFLICT (event_id) DO NOTHING` для дедупликации (см. [03-architecture.md](03-architecture.md)).
- `payload` хранит **распарсенный** объект (не сырые байты). Bearer-секрет в нём отсутствует (он в заголовке, не в теле). Дополнительно — `assert_no_secrets` на audit-пути.
- Index по `user_id` — для будущих выборок «события пользователя» (диагностика). На MVP запросов по нему в горячем пути нет.

## Затронутые существующие таблицы (без изменения схемы)
- `subscriptions` — upsert по `user_id` (status `active|expired`, plan, expires_at) для granting/expiring; **NOOP-события подписку НЕ трогают** (ADR-047). Схема: `src/app/models/tables.py:69-87`, enum `subscription_status` ∈ `none|active|expired`.
- `ledger_transactions` — грант кредитов идемпотентно по `(user_id, idempotency_key="adapty-txn:{transaction_id ‖ original_transaction_id ‖ event_id}")` (**ADR-047 — ключ по transaction_id, не по event_id**; [ADR-005](../../adr/ADR-005-idempotency-ledger.md)). Один грант на период покупки. Схема не меняется.
- `wallets` — баланс инкрементируется внутри `WalletService.grant`.
- `users` — lookup по id (`customer_user_id`).

## ORM
Добавить модель `AdaptyWebhookEvent` в `src/app/models/tables.py`. Без новых enum-типов.

## Без миграции (ADR-047)
ADR-047 **не вводит миграцию**: схема `adapty_webhook_events` неизменна (`event_id text PK` принимает значение `profile_event_id`); `will_renew`/состояние автопродления в БД **не хранится** (парсится только для audit/лога). Если в будущем потребуется персист `will_renew`/`auto_renew_status` — отдельная expand-only миграция со single-head ([Q-047-1](../../99-open-questions.md)).
