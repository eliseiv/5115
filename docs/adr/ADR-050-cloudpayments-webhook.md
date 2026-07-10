# ADR-050 — RU-биллинг через агрегатор broadapps (CloudPayments-формат вебхука)

- Статус: Accepted
- Дата: 2026-07-02
- Связано: [ADR-029](ADR-029-adapty-subscription-webhook.md) / [ADR-046](ADR-046-adapty-webhook-outcome-logging.md) / [ADR-047](ADR-047-adapty-real-payload-format-and-grant-idempotency.md) (образец webhook-контура), [ADR-005](ADR-005-idempotency-ledger.md) (идемпотентность ledger), [ADR-006](ADR-006-credit-billing-and-subscription-grant.md) (грант кредитов на период), [ADR-015](ADR-015-consumable-token-iap.md) (token-пакеты, `TOKEN_PRODUCTS`), [ADR-048](ADR-048-admin-subscription-grant.md) (upsert `subscriptions` через `ON CONFLICT(user_id)`), [ADR-002](ADR-002-access-policy-state-machine.md) (policy), [ADR-017](ADR-017-shared-server-traefik-deploy.md) (per-instance секреты). Модуль [billing-cloudpayments](../modules/billing-cloudpayments/README.md).

## Context

Появился **отдельный** путь оплаты для РФ-пользователей: платёжный агрегатор **broadapps** (`pay.broadapps.dev`) фронтит **YooKassa** и по факту успешной оплаты отправляет серверный колбэк на наш бэкенд **в формате CloudPayments** (`Data`-строка + плоские поля `AccountId`/`TransactionId`/`Status`/…). Это НЕ Adapty и НЕ StoreKit — третий, независимый источник платёжных событий.

**Инцидент/причина.** Сейчас broadapps сконфигурирован слать колбэк на наш Adapty-эндпоинт `POST /v1/billing/adapty/webhook`. Результат: `401` (bearer не совпадает с `ADAPTY_WEBHOOK_SECRET`) и, даже пройди авторизация, — несовместимый формат payload (Adapty-парсер ищет `profile_event_id`/`event_properties`, которых в CloudPayments-теле нет). Начисления не происходит. Нужен **свой** эндпоинт со своим секретом и своим парсером.

**Подтверждённые заказчиком факты (вход для решения):**
- `userId` = `AccountId` (верхний уровень) = `Data.user_id` (дублируется) — это **наш backend `userId`** (клиент передаёт его при создании платёжной ссылки). Приходит в **ВЕРХНЕМ регистре** → нормализовать к lower для сопоставления с `users`.
- Авторизация колбэка — статический `Authorization: Bearer <app_api_key>` (per-instance; на avelyra = app API key broadapps).
- Рефанды **НЕ** передаются (только успешные платежи). Обработка возвратов вне scope.
- `Data` — это **строка** с JSON (парсить отдельным `json.loads`).
- apidog-дока агрегатора запаролена (недоступна) → **точный формат ответа и коды CloudPayments приняты как допущение** (`{"code":0}` при успехе) с явной пометкой «проверить живьём» ([Q-050-1](../99-open-questions.md)).

**Реальный пример колбэка** (POST, успешная оплата годовой подписки):
```json
{
  "Data": "{\"app_id\":\"2259dcce-...\",\"user_id\":\"B284721F-C3E0-4446-B00F-3C6A21F32535\",\"cms_name\":\"yookassa_sdk_php_3\",\"product_id\":\"yearly_49.99_nottrial\",\"recurring_amount\":\"3990.00\",\"billing_interval_unit\":\"year\",\"billing_interval_count\":\"1\",\"subscription_id\":\"f95b318c-...\",\"billing_phase\":\"regular\",\"is_trial_initial\":false,\"is_trial_conversion\":false,\"is_initial_payment\":false,\"is_first_recurring_after_initial\":false,\"CloudPayments\":{\"IsApp\":false,\"AppName\":null},\"paymentGateway\":\"yookassa\"}",
  "Amount": 3990, "Issuer": "VTB", "Status": "Completed", "CardType": "Mir", "Currency": "RUB",
  "DateTime": "2026-07-02T14:09:12+00:00", "TestMode": false,
  "AccountId": "B284721F-C3E0-4446-B00F-3C6A21F32535", "Description": "Годовая подписка",
  "CardFirstSix": "220024", "CardLastFour": "8808", "OperationType": "Payment",
  "PaymentAmount": 3990, "TransactionId": "31d884c8-000f-5001-8000-1fb75b44e1d9",
  "SubscriptionId": "f95b318c-...", "PaymentCurrency": "RUB"
}
```

## Decision

Ввести **новый изолированный модуль `billing_cloudpayments`** и эндпоинт `POST /v1/billing/cloudpayments/webhook`, построенный по образцу Adapty-webhook ([ADR-029](ADR-029-adapty-subscription-webhook.md)/[ADR-046](ADR-046-adapty-webhook-outcome-logging.md)/[ADR-047](ADR-047-adapty-real-payload-format-and-grant-idempotency.md)), но со своим секретом, парсером CloudPayments-формата, таблицей дедупа и правилами маппинга. Adapty / StoreKit / BYOK **не трогаются**.

### 1. Эндпоинт и авторизация

- **`POST /v1/billing/cloudpayments/webhook`.** Имя пути отражает **wire-формат** (CloudPayments), а не имя агрегатора (broadapps) и не нижележащий шлюз (YooKassa). Обоснование: если агрегатор сменится, но формат останется CloudPayments — контракт стабилен; параллелизм с `/v1/billing/adapty/webhook`.
- Авторизация — статический `Authorization: Bearer <CLOUDPAYMENTS_WEBHOOK_TOKEN>` (per-instance env). Сравнение **constant-time** (`hmac.compare_digest`), **per-route dependency** (образец `require_adapty_webhook`), изолировано от JWT / admin / preview / Adapty-контуров.
  - Секрет **не задан** (`CLOUDPAYMENTS_WEBHOOK_TOKEN == ""`) → **`500`** (мис-конфигурация; пустой секрет не матчит ни один presented-токен). Так эндпоинт **активен только там, где секрет задан** (avelyra), на прочих инстансах — `500`.
  - Отсутствует/не совпал токен → **`401`** (причина не раскрывается).
- **Тело читается СЫРЫМ** (`await request.body()`), **без строгой Pydantic-валидации** (как Adapty). Кривой/неполный payload не должен давать `422`, иначе агрегатор может уйти в ретраи. Вся валидация — дефенсивная, в чистых функциях парсера.

### 2. Дефенсивный парсинг (чистые функции)

Плоские поля — **PascalCase** (`AccountId`, `TransactionId`, `Status`, `OperationType`, `Amount`, `Currency`, `TestMode`); поля внутри `Data` — **snake_case**.

- **`userId`** ← `AccountId` (верх) → fallback `Data.user_id`. **Нормализация к lower** (приходит в верхнем регистре), парсинг в `UUID`. Нет/не-UUID → `ignored/invalid_account_id`. Не найден в `users` → `ignored/user_not_found` (**без** создания пользователя).
- **`TransactionId`** (верх) → строка; ключ дедупа события и идемпотентности гранта. Нет → `ignored/missing_transaction_id`.
- **Гейт обработки:** обрабатывать **только если** `Status == "Completed"` (case-insensitive) **И** `OperationType == "Payment"` (case-insensitive). Иначе → `ignored/not_a_completed_payment` (`200`).
- **`Data`** — распарсить как JSON-**строку** (`json.loads`); дефенсивно принять и уже-словарь. Нераспарсиваемо/нет → `ignored/invalid_data`. Из объекта извлечь: `product_id`, `billing_interval_unit`, `billing_interval_count` (→int, дефолт 1), `billing_phase`, `subscription_id`, `is_trial_initial`/`is_trial_conversion`/`is_initial_payment` (bool, только для audit/лога).
- **`product_id`** отсутствует/пуст → `ignored/missing_product_id`.

### 3. Классификация продукта и начисление

**`classify_product(product_id, billing_interval_unit, token_product_ids: frozenset[str]) -> "subscription" | "tokens" | "unknown"`** (детерминированный порядок; обоснование ниже). `token_product_ids` передаётся **аргументом** — парсер остаётся чистым (без импорта `settings`); сервис резолвит `settings.token_products()` и передаёт его ключи как `frozenset`:
1. `product_id ∈ token_product_ids` (= ключи `token_products()`) → **`tokens`**. Явная операторская конфигурация consumable — приоритет.
2. иначе `billing_interval_unit` присутствует и непуст (`year`/`month`/`week`/`day`) → **`subscription`**. Наличие интервала рекуррентности — сильнейший структурный признак подписки.
3. иначе `product_id` матчит паттерн token-пакета (`^\d+_tokens`, case-insensitive) → **`tokens`**, но по имени; амаунт см. §3b.
4. иначе `product_id` матчит паттерн подписки (содержит `week`/`month`/`year`/`day` **или** оканчивается на `_nottrial`/`_trial`) → **`subscription`**.
5. иначе → **`unknown`** → `ignored/unknown_product` (`200`, **WARNING** в логе).

**§3a. Подписка** → активировать/продлить `subscriptions` **upsert `ON CONFLICT(user_id) DO UPDATE`** (образец [ADR-048](ADR-048-admin-subscription-grant.md)): `status='active'`, `plan=product_id`, `expires_at = now() + interval(billing_interval_unit × billing_interval_count)`; + грант кредитов периода. Продление приходит **новым `TransactionId`** (тот же `subscription_id`) → новый грант/продление (идемпотентно по `cp-txn:{TransactionId}`, §4).
- Кредиты подписки = `cloudpayments_product_tokens().get(product_id) or cloudpayments_subscription_tokens_grant` (**новая** per-tier карта + fallback, mirror `ADAPTY_PRODUCT_TOKENS`/`ADAPTY_SUBSCRIPTION_TOKENS_GRANT`). **Обоснование отдельной карты, а не flat `SUBSCRIPTION_CREDITS_PER_PERIOD`:** годовой тариф очевидно должен давать больше кредитов, чем недельный, и калибровка RU-пути независима от StoreKit/Adapty.
- `expires_at` — приближение по `timedelta` (`day=1`, `week=7`, `month=30`, `year=365` дней × count): CloudPayments-тело **не несёт** явного `expires_at` (в отличие от Adapty `subscription_expires_at`), а точный день менее важен, чем сам факт активного окна (агрегатор пришлёт продление новым `TransactionId` к реальному сроку). Календарная точность (relativedelta) — [Q-050-3](../99-open-questions.md).

**§3b. Token-пакет** → **разовое** начисление `N` кредитов, где **`N` строго из `TOKEN_PRODUCTS`** (`token_products().get(product_id)`, существующая карта [ADR-015](ADR-015-consumable-token-iap.md), anti-tamper BR-TP-1 — **никогда** из тела/из имени продукта). Product-id матчнул token-паттерн по имени, но **отсутствует** в `TOKEN_PRODUCTS` → `ignored/unknown_product` (**WARNING**; оператор обязан завести продукт). One-time; подписку НЕ трогает.

Переиспользуются существующие: `WalletService.grant`, upsert `subscriptions` (ADR-048), config `token_products()`.

### 4. Идемпотентность (разведены два механизма — как [ADR-047 §C](ADR-047-adapty-real-payload-format-and-grant-idempotency.md))

- **Дедуп события** по `TransactionId` — **новая таблица `cloudpayments_webhook_events`** (`transaction_id` **UNIQUE/PK**, `user_id`, `product_id`, `kind`, `payload` (санитизированный, §7), `processed_at`). `INSERT ... ON CONFLICT (transaction_id) DO NOTHING RETURNING` в одной транзакции; конфликт → `duplicate` без побочных эффектов. **Миграция `0014`** (`down_revision='0013'`, single head, expand-only).
- **Идемпотентность гранта** — ledger `idempotency_key = f"cp-txn:{TransactionId}"` (UNIQUE, [ADR-005](ADR-005-idempotency-ledger.md)). Namespace **изолирован** от `adapty-txn:*` / `sub-grant:*` / `admin-sub-grant:*` / token-purchase. `TransactionId` уникален на платёж (продления — новый → начисляют заново) ⇒ один грант на платёж.

### 5. userId не найден

`customer`-lookup по нормализованному UUID: нет строки в `users` → **`200` + `{"code":0}`** (обработанный `ignored/user_not_found`) + структурный лог **WARNING** (наблюдаемость: клиент обязан слать наш `userId` как `AccountId`). **НЕ** создавать пользователей (нет доверенного JWT-`sub`, [ADR-007](ADR-007-lazy-user-provisioning.md)).

### 6. Ответ (CloudPayments-стандарт — ДОПУЩЕНИЕ к живой проверке)

- **HTTP `200` c телом `{"code": 0}`** на **все обработанные** исходы (`applied` / `duplicate` / `ignored/*`) — агрегатор считает вебхук принятым и не ретраит.
- **`500`** (ретраибельно) — **только** при реальном сбое БД или незаданном секрете; агрегатор ретраит, транзакция откатывается → чистая переобработка.
- Точный формат ответа CloudPayments (нужен ли reject-код `11` на невалидный `AccountId` и т.п.) — **[Q-050-1](../99-open-questions.md)/[Q-050-2](../99-open-questions.md)**, apidog запаролена, **проверить живьём**. На MVP — `{"code":0}` на всё принятое.

### 7. Наблюдаемость и PII (образец [ADR-046](ADR-046-adapty-webhook-outcome-logging.md))

- Каждый вызов `handle()` пишет **ровно одну** структурную запись `"cloudpayments_webhook_outcome"`. **Allowlist полей:** `result`, `reason`, `transactionId`, `productId`, `userId` (наш UUID), `kind`. Уровни: `applied`/`duplicate`/технические `ignored` → INFO/DEBUG; `user_not_found`/`unknown_product` → **WARNING**.
- **ЗАПРЕЩЕНО логировать и персистить карточные данные и токен:** `CardFirstSix`, `CardLastFour`, `Issuer`, `CardType`, `Authorization`/bearer, сырой payload целиком. `cloudpayments_webhook_events.payload` хранит **только санитизированную проекцию** (allowlist: `transactionId`, `productId`, `kind`, `status`, `operationType`, `amount`, `currency`, `testMode`, `billingIntervalUnit`, `billingIntervalCount`, `billingPhase`, `subscriptionId`) — **без** PAN-фрагментов/эмитента/токена. Это отличие от Adapty (там карт-данных нет; там хранится полный parsed-объект).
- Audit-событие `cloudpayments_payment` (actor-less, `assert_no_secrets`), payload — та же санитизированная проекция + `semantics`(`subscription|tokens`), `creditsGranted`.

### 8. Совместимость и активация

- **Не трогаются:** Adapty-webhook, StoreKit `/v1/subscription/sync`, `/v1/tokens/purchase`, BYOK, LLM-абстракция, policy-engine, схема `subscriptions`/`ledger`/`wallets`.
- **Per-instance активация:** без `CLOUDPAYMENTS_WEBHOOK_TOKEN` эндпоинт `500` (как Adapty) → **активен только на avelyra** (app API key broadapps). Операторский шаг: сменить Callback URL в broadapps с `/v1/billing/adapty/webhook` на `/v1/billing/cloudpayments/webhook`.
- **Инвариант анти-double-grant:** RU-путь (CloudPayments) и Apple-пути (StoreKit sync / Adapty) используют **разные** ledger-namespace'ы (`cp-txn:*` vs `sub-grant:*`/`adapty-txn:*`) — они **не** защищают между собой. Для одного `userId` на одном инстансе должен быть один активный путь платежей; смешение путей = риск двойного начисления (митигация — контрактная/операционная, как в [ADR-029](ADR-029-adapty-subscription-webhook.md)).

### 9. Swagger-чистота ([08-api-documentation §R2ter](../08-api-documentation.md))

User-facing OpenAPI-тексты (`summary`/`description` роута, `Field`/примеры схемы `CloudPaymentsWebhookResponse`) — **без** ADR/TD/Q-ссылок и внутреннего жаргона (имена таблиц/сервисов/namespace ключей). Лаконично, для тестировщика. Backend обязан соблюдать (см. ТЗ модуля [09-testing](../modules/billing-cloudpayments/09-testing.md)/[02-api-contracts](../modules/billing-cloudpayments/02-api-contracts.md)).

## Consequences

**Плюсы:**
- RU-оплаты через broadapps/YooKassa начисляют кредиты и активируют подписки; закрыт инцидент «broadapps → Adapty-эндпоинт → 401».
- Полная изоляция от Adapty/StoreKit: отдельный секрет, парсер, таблица, namespace ledger — нулевой риск регресса существующих путей.
- Карточные PII не логируются и не персистятся (санитизированная проекция), audit безопасен.
- Дедуп события + идемпотентность гранта разведены — устойчивость к повторной доставке и к нескольким событиям одного платежа.

**Минусы / риски / долг:**
- Точный формат ответа CloudPayments — **допущение** (`{"code":0}`), требует живой проверки ([Q-050-1](../99-open-questions.md)). Если агрегатор ждёт иной shape/коды — правка ответа (без слома контракта начисления).
- `expires_at` — приближённое (timedelta-days), не календарно-точное ([Q-050-3](../99-open-questions.md)).
- Трактовка trial (`is_trial_*`, нулевой amount) — на MVP грант на **каждый** Completed Payment (идемпотентно по `TransactionId`); trial→conversion как два `TransactionId` могут начислить дважды за один период — [Q-050-4](../99-open-questions.md).
- Ещё один webhook-секрет в secret-manager (per-instance).

## Alternatives (отвергнуто)

- **Расширить Adapty-эндпоинт под CloudPayments-формат.** Отвергнуто: другой формат, другой секрет, другая семантика (карт-данные/санитизация) — смешение раздуло бы парсер Adapty и создало риск регресса основного пути. Изоляция дешевле.
- **Парсить N токенов из имени продукта (`100_tokens`).** Отвергнуто: имя продукта клиент-контролируемо → anti-tamper требует брать сумму только из серверной карты `TOKEN_PRODUCTS` ([ADR-015](ADR-015-consumable-token-iap.md) BR-TP-1).
- **Reject невалидного `AccountId` кодом CloudPayments (11).** Отложено ([Q-050-2](../99-open-questions.md)): формат кодов не подтверждён (apidog запаролена); на MVP — `200 {"code":0}` + WARNING, чтобы не спровоцировать ретраи/ошибку оплаты у уже заплатившего пользователя.
- **Flat `SUBSCRIPTION_CREDITS_PER_PERIOD` для подписок.** Отвергнуто в пользу per-tier карты (`CLOUDPAYMENTS_PRODUCT_TOKENS`): годовой ≠ недельный по кредитам.
