# billing-adapty / 02 — API Contracts

## POST /v1/billing/adapty/webhook

Серверный вебхук Adapty. **Вызывает Adapty**, не iOS-клиент. Контракт целиком в [ADR-029](../../adr/ADR-029-adapty-subscription-webhook.md); **реальный формат payload, маппинг событий и идемпотентность гранта исправлены в [ADR-047](../../adr/ADR-047-adapty-real-payload-format-and-grant-idempotency.md)** (по реальным payload'ам Adapty).

### Авторизация
- `Authorization: Bearer <ADAPTY_WEBHOOK_SECRET>` — статический секрет, заданный оператором в Adapty UI.
- НЕ пользовательский JWT, НЕ `X-Admin-Token`. Отдельный контур (третий тип авторизации; добавить в [API-REFERENCE §2](../../API-REFERENCE.md)).
- Сравнение constant-time (`hmac.compare_digest`). Неверный/нет токена → `401`. Секрет не сконфигурирован (`ADAPTY_WEBHOOK_SECRET` пуст) → `500`.

### Тело запроса
- **Без схемы / без Pydantic-валидации.** Читается сырое (`await request.body()`). Adapty при сохранении вебхука шлёт проверочный пинг с пустым/не-JSON/неполным телом — он обязан получить `2xx`.

#### Реальный формат payload (ADR-047, по факту прода)

Одна покупка генерирует **несколько** событий с **разными** `profile_event_id`, но **одним** `transaction_id`. Поля Adapty в wire-формате типично лежат в `event_properties` (`ep`); Dashboard-вид показывает их «расплющенными» (top-level). **Точная wire-структура (`event_type` плоский vs в обёртке) на 100% не подтверждена** — парсинг дефенсивный (см. [03-architecture.md](03-architecture.md)); финальная сверка — по логам [ADR-046](../../adr/ADR-046-adapty-webhook-outcome-logging.md) после деплоя.

Реальные ключевые поля (пример — недельная подписка `week_6.99_nottrial`, free-trial по промо `ytl`):
```json
{
  "event_type": "trial_started | access_level_updated | subscription_renewal_cancelled | subscription_started | subscription_renewed | subscription_expired | ...",
  "event_properties": {
    "profile_event_id": "a3254174-74a4-4597-82a0-83d9ebfd2cf0",
    "vendor_product_id": "week_6.99_nottrial",
    "subscription_expires_at": "2026-07-07T09:05:46Z",
    "transaction_id": 410003298316682,
    "original_transaction_id": 410003298316682,
    "is_active": true,
    "access_level_id": "premium",
    "will_renew": false,
    "profile_has_access_level": true,
    "profile_id": "3bf27b33-2866-4161-85c4-48bae895d7c4",
    "store": "app_store"
  }
}
```
- `event_id` нашего журнала ← **`profile_event_id`** (не `event_id`/`id` — их в payload нет).
- `customer_user_id` (наш `userId` UUID) **отсутствует** в текущих payload'ах; появится, когда iOS вызовет `Adapty.identify(<userId>)`. До этого → `200 ignored/missing_customer_user_id` (корректно, видно в логах ADR-046).
- `transaction_id`/`original_transaction_id`/`profile_event_id` могут приходить **числом** (без кавычек) — парсер приводит к строке.

Полный порядок fallback-источников по каждому полю — [03-architecture.md §Дефенсивный парсинг](03-architecture.md).

#### Маппинг событий (ADR-047)

| `event_type` | Семантика | `subscriptions` | Кредиты |
|---|---|---|---|
| `trial_started` / `subscription_started` / `subscription_renewed` | GRANTING | `active`, `plan`, `expires_at` | **грант** (идемпотентно по `transaction_id`) |
| `access_level_updated` + `is_active=true` + `access_level_id="premium"` | GRANTING | `active`, `plan`, `expires_at` | **грант** |
| `subscription_expired` / `subscription_cancelled` | EXPIRING | `expired` | не трогаем |
| `access_level_updated` + `is_active=false` | EXPIRING | `expired` | не трогаем |
| `subscription_renewal_cancelled` / `trial_renewal_cancelled` | **NOOP** | **не трогаем** (доступ сохраняется) | не трогаем |
| `access_level_updated` + `is_active=true` + не-`premium` (или `is_active` неизвестен) | NOOP | не трогаем | не трогаем |
| прочее | UNKNOWN | — | `200 ignored` (+эхо `event_type`) |

**NOOP** (отмена автопродления, `profile_has_access_level=true`, `will_renew=false`) — доступ **НЕ отзывается**; событие записывается (дедуп) + audit, но без мутации подписки/кредитов. Старые имена полей сохранены как fallback.

### Ответы

Все `200` кроме `401`/`500`. Тело: `{ "result": <...>, "reason"?: <...>, "event_type"?: <...> }`.

| HTTP | Тело | Когда |
|---|---|---|
| 401 | (ошибка авторизации) | нет/неверный bearer |
| 500 | (ошибка мис-конфигурации) | `ADAPTY_WEBHOOK_SECRET` не задан |
| 200 | `{"result":"ignored","reason":"empty_body"}` | пустое тело (проверочный пинг) |
| 200 | `{"result":"ignored","reason":"invalid_json"}` | не-JSON |
| 200 | `{"result":"ignored","reason":"not_an_object"}` | JSON не объект |
| 200 | `{"result":"ignored","reason":"missing_event_id"}` | нет `event_id` |
| 200 | `{"result":"ignored","reason":"missing_customer_user_id"}` | нет/не-UUID `customer_user_id` |
| 200 | `{"result":"ignored","reason":"user_not_found"}` | пользователь не найден |
| 200 | `{"result":"ignored","event_type":"<echo>"}` | неизвестный `event_type` |
| 200 | `{"result":"duplicate"}` | повтор `event_id` |
| 200 | `{"result":"applied"}` | событие применено |
| 500 | (внутренний сбой) | БД недоступна и т. п. → Adapty ретраит |

### Эффекты при `applied`
- GRANTING: `subscriptions.status=active`, `plan=vendor_product_id`, `expires_at` (из `subscription_expires_at`); грант кредитов по тиру, **идемпотентно по `adapty-txn:{transaction_id}`** (ADR-047, не по `event_id`).
- EXPIRING: `subscriptions.status=expired`; кредиты не изменяются.
- NOOP (`*_renewal_cancelled`): подписка/кредиты **не изменяются** (доступ сохраняется); событие записано + audit.

### Идемпотентность (ADR-047 — разведены два механизма)
- **Дедуп события:** повтор `event_id` (= `profile_event_id`) → `duplicate` без побочных эффектов (UNIQUE `adapty_webhook_events.event_id`). Защищает от повторной доставки **того же** события.
- **Идемпотентность начисления:** ledger `idempotency_key = adapty-txn:{transaction_id ‖ original_transaction_id ‖ event_id}` (UNIQUE, [ADR-005](../../adr/ADR-005-idempotency-ledger.md)). Гарантирует **один грант на период покупки**, сколько бы granting-событий период ни сгенерировал. `transaction_id` (per-период) первичен; `original_transaction_id` НЕ первичен (постоянен на всю цепочку → продления не начисляли бы). Подробности — [ADR-047 §C](../../adr/ADR-047-adapty-real-payload-format-and-grant-idempotency.md).
