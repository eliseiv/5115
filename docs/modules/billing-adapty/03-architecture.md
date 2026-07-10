# billing-adapty / 03 — Architecture

Реализует [ADR-029](../../adr/ADR-029-adapty-subscription-webhook.md); **парсинг реального формата, маппинг событий и идемпотентность гранта — [ADR-047](../../adr/ADR-047-adapty-real-payload-format-and-grant-idempotency.md)** (заменяет §«Дефенсивный парсинг», §«Маппинг событий», §«Grant» ниже). Ниже — детали для backend.

## Файлы (фактическая раскладка)
- `src/app/billing_adapty/__init__.py`
- `src/app/api_gateway/routers/billing_adapty.py` — эндпоинт `POST /v1/billing/adapty/webhook` (`router`, prefix `/v1/billing/adapty`, tag `Billing (Adapty)`), per-route bearer-dependency, сырое тело, делегирование в `AdaptyWebhookService`. (Роутеры проекта живут в `api_gateway/routers/`, не в пакете модуля.)
- `src/app/billing_adapty/auth.py` — `require_adapty_webhook` (constant-time bearer) + `AdaptyWebhookMisconfiguredError` (override `status_code = 500`, code `adapty_webhook_misconfigured`). 401 при mismatch/нет токена, 500 при незаданном секрете.
- `src/app/billing_adapty/service.py` — `AdaptyWebhookService` + `WebhookOutcome`: дедуп, маппинг события, транзакция (upsert subscription + grant + audit).
- `src/app/billing_adapty/parser.py` — чистые функции дефенсивного парсинга полей + `ParsedEvent`, константы событий (`GRANTING_EVENTS`/`EXPIRING_EVENTS`/`KNOWN_EVENTS`).
- `src/app/schemas/billing_adapty.py` — `AdaptyWebhookResponse` (`{result, reason?, event_type?}`, только для OpenAPI; тела запроса нет).
- OpenAPI-схема `adapty_webhook_scheme` (`HTTPBearer`, `scheme_name="adaptyWebhook"`, `auto_error=False`) — в `src/app/api_gateway/openapi_security.py`.
- Фабрика `get_adapty_webhook_service` — в `src/app/deps.py`.
- Регистрация роутера в `src/app/main.py` (`include_router`).

## Поток обработки

```mermaid
sequenceDiagram
    participant A as Adapty
    participant R as Router (/v1/billing/adapty/webhook)
    participant S as AdaptyWebhookService
    participant DB as PostgreSQL (1 транзакция)
    A->>R: POST + Authorization: Bearer <secret>
    R->>R: constant-time compare (401 / 500-if-unset)
    R->>R: raw = await request.body()  (без Pydantic)
    R->>S: handle(raw)
    S->>S: parse: empty/json/object/event_id/customer_user_id
    alt любая невалидность
        S-->>R: 200 ignored/<reason>
    else валидное событие
        S->>DB: BEGIN
        S->>DB: INSERT adapty_webhook_events ON CONFLICT(event_id) DO NOTHING RETURNING event_id
        alt конфликт (дубликат) — RETURNING пуст
            Note over S,DB: мутаций нет (никакого audit/grant/upsert); транзакция фиксируется без изменений
            S-->>R: 200 duplicate
        else вставлено
            S->>DB: classify_event → upsert subscriptions (active|expired) | noop (без изменений)
            opt GRANTING (trial/started/renewed/access_level@premium)
                S->>DB: WalletService.grant(idem="adapty-txn:{transaction_id}")
            end
            S->>DB: audit adapty_subscription (assert_no_secrets)
            S->>DB: COMMIT
            S-->>R: 200 applied
        end
    end
    Note over S,DB: любой сбой → ROLLBACK → 500 → Adapty ретраит → чистая переобработка
```

## Авторизация (детали для backend)
- Заголовок `Authorization: Bearer <token>`. Извлечь токен (после `Bearer `), `hmac.compare_digest(token, settings.adapty_webhook_secret)`.
- `settings.adapty_webhook_secret == ""` → **500** (мис-конфигурация, текст вида `"adapty webhook secret not configured"`). Пустой секрет не матчит ни один presented-токен.
- Mismatch / нет заголовка → **401** (`UnauthorizedError`, без раскрытия причины).
- Реализовать **per-route** (Depends), не глобальным middleware. Эндпоинт изолирован от пользовательской JWT-цепочки и от admin-токена.
- OpenAPI: завести отдельную security-схему (http bearer) по образцу `admin_scheme` (`auto_error=False`), чтобы Swagger показал «Authorize» без дублирующего header-параметра.

## Матрица ответов (точные коды/статусы)
Все `200`, кроме явно `401`/`500`. Тело JSON `{result, reason?, event_type?}`.

| Условие | HTTP | `result` | `reason` |
|---|---|---|---|
| нет/неверный bearer | 401 | — | — |
| секрет не задан | 500 | — | — |
| пустое тело | 200 | `ignored` | `empty_body` |
| не-JSON | 200 | `ignored` | `invalid_json` |
| JSON не объект | 200 | `ignored` | `not_an_object` |
| нет `event_id` | 200 | `ignored` | `missing_event_id` |
| нет `customer_user_id` (или не UUID) | 200 | `ignored` | `missing_customer_user_id` |
| пользователь не найден | 200 | `ignored` | `user_not_found` |
| неизвестный `event_type` | 200 | `ignored` | (+ `event_type` эхо) |
| дубликат `event_id` | 200 | `duplicate` | — |
| валидное событие | 200 | `applied` | — |
| внутренний сбой (БД и т. п.) | 500 | — | — |

## Дефенсивный парсинг (порядок источников, ADR-047 — ЗАМЕНЯЕТ ADR-029 §3)

`ep = body.event_properties` (основной носитель бизнес-полей в wire-формате Adapty; плоские top-level ключи — fallback под Dashboard-вид/старые версии). Первое непустое значение выигрывает; вложенный доступ `isinstance(..., dict)`-guarded; отсутствует → `None`. Строковый хелпер принимает `int`→`str(int)` (id-поля приходят числом). `is_active`/`will_renew` — строго `bool` (иначе `None`).

- `event_id = profile_event_id ‖ ep.profile_event_id ‖ event_id ‖ id` (**NEW: `profile_event_id` первым**)
- `event_type = (event_type ‖ event ‖ ep.event_type ‖ type).lower()` (дефенсивно, wire-структура не подтверждена 100%)
- `customer_user_id = customer_user_id ‖ profile.customer_user_id ‖ ep.customer_user_id ‖ user_id` → UUID. Не-UUID/нет → `ignored/missing_customer_user_id` (сейчас — норма, iOS ещё не вызвал `Adapty.identify`).
- `vendor_product_id = ep.vendor_product_id ‖ ep.product_id ‖ vendor_product_id ‖ product_id`
- `expires_at = ep.subscription_expires_at ‖ ep.expires_at ‖ subscription_expires_at ‖ expires_at ‖ profile.expires_at` (ISO8601→tz-aware; нераспарсиваемое→`None`, **NEW: `subscription_expires_at` первым**)
- `transaction_id = ep.transaction_id ‖ transaction_id` (**NEW**, →str)
- `original_transaction_id = ep.original_transaction_id ‖ original_transaction_id` (**NEW**, →str)
- `is_active = ep.is_active ‖ is_active` (**NEW**, строго bool|None)
- `access_level_id = ep.access_level_id ‖ access_level_id` (**NEW**)
- `will_renew = ep.will_renew ‖ will_renew` (**NEW**, bool|None; **audit/лог only, в БД НЕ хранится**)

`ParsedEvent` расширяется новыми полями (`transaction_id`, `original_transaction_id`, `is_active`, `access_level_id`, `will_renew`).

## Маппинг событий (ADR-047 — ЗАМЕНЯЕТ ADR-029 §4; диспетчер `classify_event(ParsedEvent) -> Semantics`)

`access_level_updated` разрешается условно (по `is_active`/`access_level_id`), поэтому не чистый frozenset-lookup.

| `event_type` (+условие) | Семантика | `subscriptions` | grant |
|---|---|---|---|
| `trial_started` / `subscription_started` / `subscription_renewed` | GRANTING | `active`, `plan=vendor_product_id`, `expires_at` | **да** (тир) |
| `access_level_updated` + `is_active=true` + `access_level_id="premium"` | GRANTING | как выше | **да** |
| `subscription_expired` / `subscription_cancelled` | EXPIRING | `expired` (plan/expires_at без изм.) | нет |
| `access_level_updated` + `is_active=false` | EXPIRING | `expired` | нет |
| `subscription_renewal_cancelled` / `trial_renewal_cancelled` | **NOOP** | **без изменений** (доступ сохраняется) | нет |
| `access_level_updated` + `is_active=true` + не-`premium` / `is_active=None` | NOOP | без изменений | нет |
| прочее (∉ `KNOWN_EVENTS`) | UNKNOWN | — | `200 ignored` (+эхо `event_type`) |

`KNOWN_EVENTS = GRANTING_EVENTS ∪ EXPIRING_EVENTS ∪ NOOP_EVENTS ∪ {access_level_updated}`. NOOP-событие **записывается** в `adapty_webhook_events` (дедуп) + audit, но **без** мутации `subscriptions` и без гранта (доступ при отмене автопродления НЕ отзывается).

## Транзакционность
Вся обработка распознанного события — **одна** транзакция (`async with session.begin()` или эквивалент). INSERT `adapty_webhook_events ... ON CONFLICT (event_id) DO NOTHING RETURNING event_id` (`event_id` = `profile_event_id`) — точка дедупликации **доставки события**:
- `RETURNING` пуст → дубликат → `200 duplicate`, мутаций нет.
- иначе → `classify_event` → upsert subscription (granting/expiring) или no-op (noop) → (для granting) `grant` → audit → commit.
Сбой на любом шаге → ROLLBACK → 500 → Adapty ретраит. На ретрае `event_id` свободен (INSERT откатился) ⇒ чистая переобработка; `grant` дополнительно идемпотентен по txn-ключу (ниже).

## Тир product → tokens
```
tokens = settings.adapty_product_tokens().get(vendor_product_id) or settings.adapty_subscription_tokens_grant
```
`adapty_product_tokens()` — хелпер `Settings` (`config.py:314`, уже реализован): парсит `ADAPTY_PRODUCT_TOKENS` (JSON `{str: positive-int}`), малформед → `{}`. `adapty_subscription_tokens_grant` — int из `ADAPTY_SUBSCRIPTION_TOKENS_GRANT` (дефолт 1000). `week_6.99_nottrial` оператор может добавить в карту (без деплоя); иначе fallback 1000.

## Grant (ADR-047 — ключ идемпотентности по transaction_id, НЕ по event_id)
```
txn = event.transaction_id or event.original_transaction_id or event.event_id
WalletService.grant(
    user_id=<UUID customer_user_id>,
    amount=tokens,
    idempotency_key=f"adapty-txn:{txn}",
    reason="adapty_subscription",
    meta={"transactionId": txn, "eventType": event_type, "vendorProductId": vendor_product_id},
)
```
**`transaction_id` первичен** (уникален на период → продления начисляют заново; несколько событий одного периода → один грант). `original_transaction_id` — fallback (постоянен на цепочку, НЕ первичен — иначе продления без кредитов). `event_id` — крайний fallback (вырожденный случай без transaction id). Только для granting-событий. Подробности и обоснование — [ADR-047 §C](../../adr/ADR-047-adapty-real-payload-format-and-grant-idempotency.md).

## Observability (логирование исхода, [ADR-046](../../adr/ADR-046-adapty-webhook-outcome-logging.md))
Каждый вызов `handle()` пишет **ровно одну** структурную запись `"adapty_webhook_outcome"` в сервисе (`log_event`, образец `app.chat.orchestrator`): allowlist полей `result`/`reason`/`eventType`/`eventId`/`customerUserId`, уровни `INFO`/`WARNING`/`DEBUG` по исходу (`user_not_found`/`missing_customer_user_id`/unknown-type → WARNING как «потенциально потерянное начисление»). Точное ТЗ backend (сигнатуры, decision-таблица, точки вызова, PII-allowlist) — [08-observability.md](08-observability.md). HTTP-семантика/начисление/`KNOWN_EVENTS`/контракт не меняются.

## Audit
Новое `EVENT_ADAPTY_SUBSCRIPTION = "adapty_subscription"` в `src/app/audit/service.py`.

**Audit пишется ТОЛЬКО на `applied`** (внутри `_apply`, после успешного INSERT-дедупа и upsert/grant). На `ignored` (любой `reason`, включая `user_not_found` и неизвестный `event_type`) и на `duplicate` — audit **НЕ пишется** (никаких мутаций). Это исключает шум аудита от проверочных пингов Adapty и от повторных доставок.

Запись (на `applied` для granting/expiring/noop). ADR-047 расширяет payload полями `transactionId`, `semantics`, опц. `willRenew`:
```
AuditEvent(user_id=<uuid>, event_type=EVENT_ADAPTY_SUBSCRIPTION, payload={
    "adaptyEventId": event_id, "eventType": event_type, "semantics": <granting|expiring|noop>,
    "status": status, "plan": plan, "expiresAt": <iso|null>,
    "transactionId": <txn|null>, "willRenew": <bool|null>, "customerId": str(user_id)})
```
`assert_no_secrets` уже применяется внутри `AuditService.record` (новые поля не-секреты). Bearer-секрет в payload не кладём.
