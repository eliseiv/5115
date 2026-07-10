# billing-adapty / 07 — Implementation Phases

Реализация [ADR-029](../../adr/ADR-029-adapty-subscription-webhook.md). Backend выполняет фазы строго по порядку.

## Фаза 1 — Config + миграция
- `src/app/config.py`: добавить
  - `adapty_webhook_secret: str = Field(default="", alias="ADAPTY_WEBHOOK_SECRET")`
  - `adapty_product_tokens_raw: str = Field(default="{}", alias="ADAPTY_PRODUCT_TOKENS")`
  - `adapty_subscription_tokens_grant: int = Field(default=1000, alias="ADAPTY_SUBSCRIPTION_TOKENS_GRANT")`
  - метод `adapty_product_tokens() -> dict[str, int]` по образцу `token_products()` (`config.py:199`): JSON `{str: positive-int}`, малформед → `{}`, bool исключить.
- Миграция **`0008`** (после `0007`): таблица `adapty_webhook_events` (DDL — [04-data-model.md](04-data-model.md)) + index по `user_id`. ORM-модель `AdaptyWebhookEvent` в `src/app/models/tables.py`.
- Audit: `EVENT_ADAPTY_SUBSCRIPTION = "adapty_subscription"` в `src/app/audit/service.py`.

## Фаза 2 — Авторизация
- `require_adapty_webhook` (constant-time bearer, образец `auth.py:99-134`): извлечь токен после `Bearer `, `compare_digest` с `settings.adapty_webhook_secret`; пустой секрет → `500`; mismatch/нет → `401`.
- OpenAPI security-схема (http bearer, `auto_error=False`), образец `admin_scheme` (`openapi_security.py`).

## Фаза 3 — Парсинг + сервис
- Дефенсивный парсинг полей (точные источники — [03-architecture.md](03-architecture.md)): `event_id`, `event_type→lower`, `customer_user_id→UUID`, `vendor_product_id`, `expires_at→ISO8601`.
- `AdaptyWebhookService.handle(raw: bytes)`: матрица `ignored/*` (пустое/не-JSON/не-объект/missing/user_not_found) до транзакции.
- Тир: `adapty_product_tokens().get(vendor_product_id) or adapty_subscription_tokens_grant`.

## Фаза 4 — Транзакция
- Одна транзакция: `INSERT adapty_webhook_events ... ON CONFLICT (event_id) DO NOTHING RETURNING event_id` → пусто ⇒ `duplicate`; иначе upsert `subscriptions` (active|expired) + (для granting) `WalletService.grant(...)` + audit. Commit. Сбой → ROLLBACK → 500.
- **⚠ Ключ гранта и маппинг событий ОБНОВЛЕНЫ в Фазе 8 ([ADR-047](../../adr/ADR-047-adapty-real-payload-format-and-grant-idempotency.md)):** грант идемпотентен по `adapty-txn:{transaction_id}` (НЕ `adapty-event:{event_id}`); семантика — `classify_event` (GRANTING/EXPIRING/NOOP), а не `event_type ∈ {started,renewed}`. Реализовывать по Фазе 8.

## Фаза 5 — Router + регистрация
- `POST /v1/billing/adapty/webhook`: сырое тело (`await request.body()`), без Pydantic body-модели; per-route bearer Depends; матрица ответов (точные коды — [02-api-contracts.md](02-api-contracts.md)).
- Регистрация роутера в `src/app/main.py` (`include_router`, рядом со строками 196-212).
- `SizeLimitMiddleware` — стандартный лимит тела достаточен (payload подписки невелик); повышенный лимит роута НЕ требуется.

## Фаза 7 — Наблюдаемость (логирование исхода, [ADR-046](../../adr/ADR-046-adapty-webhook-outcome-logging.md))
- Только `src/app/billing_adapty/service.py` (роутер не трогать). Логгер `logging.getLogger(__name__)` + `log_event(...)`, образец `app.chat.orchestrator`.
- Helper `_log_outcome(outcome, *, event_type, event_id, customer_user_id)` + функция `_level_for(result, reason)`; маршрутизировать каждую return-точку `handle()`/`_apply()` через helper (ровно один лог на вызов).
- Точные сигнатуры, decision-таблица уровней, точки вызова, allowlist полей и None-обработка — [08-observability.md](08-observability.md). Без миграции, без новых env, контракт неизменен.

## Фаза 8 — Реальный формат payload, маппинг, идемпотентность гранта ([ADR-047](../../adr/ADR-047-adapty-real-payload-format-and-grant-idempotency.md))

Исправляет «слепой» парсер ADR-029 по реальным payload'ам Adapty. **Без миграции, без новых env, контракт/HTTP-семантика неизменны.** Точные правила — [03-architecture.md](03-architecture.md) (§Дефенсивный парсинг / §Маппинг событий / §Grant), [ADR-047](../../adr/ADR-047-adapty-real-payload-format-and-grant-idempotency.md).

### 8.1 — `src/app/billing_adapty/parser.py`
- **`parse_event_id`**: вернуть первое непустое из `profile_event_id` → `event_properties.profile_event_id` → `event_id` → `id`. (Было: `event_id` → `id`.)
- **`parse_event_type`**: первое непустое из `event_type` → `event` → `event_properties.event_type` → `type`, затем `.lower()`. (Дефенсивно, wire-структура не подтверждена.)
- **`parse_customer_user_id`**: добавить `event_properties.customer_user_id` в цепочку (после `profile.customer_user_id`, перед `user_id`). UUID-парсинг как есть.
- **`parse_vendor_product_id`**: без изменений (уже `event_properties.*` первым).
- **`parse_expires_at`**: добавить `event_properties.subscription_expires_at` (первым) и `subscription_expires_at` (top-level) перед существующими `event_properties.expires_at` / `profile.expires_at`; добавить top-level `expires_at`.
- **NEW `parse_transaction_id(body) -> str | None`**: `event_properties.transaction_id` → `transaction_id`. Принять `int` → `str`.
- **NEW `parse_original_transaction_id(body) -> str | None`**: `event_properties.original_transaction_id` → `original_transaction_id`. Принять `int` → `str`.
- **NEW `parse_is_active(body) -> bool | None`**: `event_properties.is_active` → `is_active`, **строго `isinstance(x, bool)`** (иначе `None`).
- **NEW `parse_access_level_id(body) -> str | None`**: `event_properties.access_level_id` → `access_level_id`.
- **NEW `parse_will_renew(body) -> bool | None`**: `event_properties.will_renew` → `will_renew`, строго `bool` (для audit/лога; в БД не пишется).
- **Хелпер выбора строки** (`_first_str`): расширить, чтобы принимать `int` и приводить к `str(int)` (id-поля приходят числом). НЕ применять это к `is_active`/`will_renew` (там строго bool).
- **Константы событий** (заменить прежние):
  - `GRANTING_EVENTS = frozenset({"trial_started", "subscription_started", "subscription_renewed"})`
  - `EXPIRING_EVENTS = frozenset({"subscription_expired", "subscription_cancelled"})`
  - `NOOP_EVENTS = frozenset({"subscription_renewal_cancelled", "trial_renewal_cancelled"})`
  - `CONDITIONAL_EVENTS = frozenset({"access_level_updated"})`
  - `KNOWN_EVENTS = GRANTING_EVENTS | EXPIRING_EVENTS | NOOP_EVENTS | CONDITIONAL_EVENTS`
  - Семантика-константы: `SEM_GRANTING = "granting"`, `SEM_EXPIRING = "expiring"`, `SEM_NOOP = "noop"`.
- **NEW `classify_event(event: ParsedEvent) -> str`** (возвращает одну из SEM_*):
  - `event_type ∈ GRANTING_EVENTS` → `SEM_GRANTING`
  - `event_type ∈ EXPIRING_EVENTS` → `SEM_EXPIRING`
  - `event_type ∈ NOOP_EVENTS` → `SEM_NOOP`
  - `event_type == "access_level_updated"`:
    - `is_active is True and access_level_id == "premium"` → `SEM_GRANTING`
    - `is_active is False` → `SEM_EXPIRING`
    - иначе (`is_active` True но не premium, либо `is_active is None`) → `SEM_NOOP`
  - (вне `KNOWN_EVENTS` сюда не доходит — отсекается раньше в `handle()`.)
- **`ParsedEvent`** (расширить dataclass): добавить поля `transaction_id: str | None`, `original_transaction_id: str | None`, `is_active: bool | None`, `access_level_id: str | None`, `will_renew: bool | None`.

### 8.2 — `src/app/billing_adapty/service.py`
- **`handle()`**: `event_id` теперь приходит из `profile_event_id` (через обновлённый `parse_event_id`) — менять код вызова не нужно, только парсер. **Рекомендуется** (синергия ADR-046): вызвать `parse_event_type(body)` **до** проверки `customer_user_id` и передать `event_type or None` в `_log_outcome(...)` ветки `missing_customer_user_id`, чтобы лог нёс тип события. Сборку `ParsedEvent` дополнить новыми полями (`parse_transaction_id`/`parse_original_transaction_id`/`parse_is_active`/`parse_access_level_id`/`parse_will_renew`).
- **`_apply()`**: после успешного дедуп-INSERT вычислить `semantics = parser.classify_event(event)` и ветвиться:
  - `SEM_GRANTING`: `_upsert_subscription` → active; `_grant(event)`; audit(`semantics="granting"`).
  - `SEM_EXPIRING`: `_upsert_subscription` → expired; **без гранта**; audit(`semantics="expiring"`).
  - `SEM_NOOP`: **НЕ** вызывать `_upsert_subscription`, **НЕ** вызывать `_grant`; audit(`semantics="noop"`, текущий status подписки если читаем, иначе `null`). Результат всё равно `applied` (событие записано).
  - Передавать `semantics` в `_upsert_subscription` (или развести логику явно по семантике, не по `event_type ∈ GRANTING_EVENTS`).
- **`_upsert_subscription()`**: ветвление по `semantics` (granting → active+plan+expires_at; expiring → expired). NOOP сюда не заходит.
- **`_grant()`**: ключ идемпотентности `f"adapty-txn:{txn}"`, где `txn = event.transaction_id or event.original_transaction_id or event.event_id`. `meta` → `{"transactionId": txn, "eventType": event.event_type, "vendorProductId": event.vendor_product_id}`.
- **Audit payload**: добавить `semantics`, `transactionId` (=txn), `willRenew` (=`event.will_renew`).

### 8.3 — Что НЕ трогать
- Авторизацию (`require_adapty_webhook`), сырое тело, HTTP-матрицу ответов (всё `200` кроме `401`/`500`-misconfig/`500`-DB-сбой).
- Структуру лога ADR-046 (`"adapty_webhook_outcome"`, allowlist полей, уровни) — только опц. передача `event_type` в ветку `missing_customer_user_id`.
- Схему БД (`adapty_webhook_events`), таблицу/миграции (миграции **нет**), `config.py` (env уже есть).
- `/v1/subscription/sync`, `/v1/tokens/purchase`, провайдер-абстракцию LLM.
- `will_renew` в БД не писать (audit/лог only).

### 8.4 — Тестовые ориентиры (для qa, в дополнение к Фазе 3-5)
- `parse_event_id` ← `profile_event_id` (в т.ч. число `int`); fallback `event_id`/`id`.
- Реальный payload (три события одной покупки): `trial_started` + `access_level_updated`(is_active=true,premium) → **ровно ОДИН** ledger-грант (ключ `adapty-txn:{transaction_id}`), подписка `active`, `expires_at` из `subscription_expires_at`.
- `subscription_renewal_cancelled` / `trial_renewal_cancelled` → подписка/баланс **не изменились**, событие записано, audit `semantics=noop`.
- `access_level_updated` is_active=false → `expired`, баланс не изменился.
- Идемпотентность: два granting-события одного `transaction_id`, но разные `profile_event_id` → один грант; повтор того же `profile_event_id` → `duplicate`.
- Продление (`subscription_renewed` с новым `transaction_id`, тем же `original_transaction_id`) → **новый** грант (НЕ схлопывается).
- `customer_user_id` отсутствует (только `profile_id`) → `ignored/missing_customer_user_id` (после фикса парсера — не `missing_event_id`).
- Тариф: `week_6.99_nottrial` в `ADAPTY_PRODUCT_TOKENS` → точное число; вне карты → fallback 1000.

## Фаза 6 — Deployment (devops, после backend)
- Завести env `ADAPTY_WEBHOOK_SECRET` (per-instance, secret manager), `ADAPTY_PRODUCT_TOKENS`, `ADAPTY_SUBSCRIPTION_TOKENS_GRANT` — см. [07-deployment.md](../../07-deployment.md). Внести в prod-checklist (high-entropy секрет, разный на инстанс).
- **ADR-047 (операторский конфиг тарифа):** добавить реальный `vendor_product_id` в `ADAPTY_PRODUCT_TOKENS`, например `{"week_6.99_nottrial": <tokens>}` — иначе период начислит fallback `ADAPTY_SUBSCRIPTION_TOKENS_GRANT` (1000). Ключ — точно как `vendor_product_id` (с точкой/подчёркиваниями). Без деплоя кода.
- **ADR-047 (сверка по логам):** после деплоя проверить логи `"adapty_webhook_outcome"` ([ADR-046](../../adr/ADR-046-adapty-webhook-outcome-logging.md)): до релиза iOS с `Adapty.identify` ожидается `missing_customer_user_id` (WARNING); реальные `event_type` сверить с `KNOWN_EVENTS` (закрытие [Q-029-3](../../99-open-questions.md)).

## Тестовые ориентиры (для qa)
- 401 на нет/неверный bearer; 500 на незаданный секрет.
- 200 `ignored/*` на каждый невалидный случай (включая проверочный пинг — пустое тело).
- `applied` для started/renewed (+ грант, + subscription active); `applied` для cancelled/expired (status expired, баланс не изменился).
- `duplicate` на повтор `event_id` (баланс не изменился — двойная UNIQUE-граница).
- Тир: vendor_product_id из карты → точное число; вне карты → fallback grant.
- Дефенсивный парсинг альтернативных имён полей (`id`, `profile.customer_user_id`, `product_id`).
