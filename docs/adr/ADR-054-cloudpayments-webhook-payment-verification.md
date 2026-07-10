# ADR-054 — Верификация RU-платежей через broadapps API как единственный триггер начисления (публичный вебхук, реконсиляция payments)

- Статус: Accepted
- Дата: 2026-07-03
- Тип: bugfix / security ADR. **Пересматривает [ADR-050 §1..§6](ADR-050-cloudpayments-webhook.md)** (авторизация, триггер, классификация и идемпотентность входящего вебхука) и **отменяет блокирующую семантику [ADR-052](ADR-052-cloudpayments-webhook-lenient-auth-header.md)** (401 больше не выдаётся; тела ADR-050/052 не переписываются — immutability, актуальное поведение здесь). **Закрывает [Q-052-1](../99-open-questions.md)** (диагностика подтвердила: broadapps шлёт колбэк БЕЗ авторизации, `authScheme=none`).
- Связано: [ADR-051](ADR-051-cloudpayments-checkout-payment-link.md) (исходящий httpx-клиент к broadapps, конфиги `CLOUDPAYMENTS_API_BASE`/`CLOUDPAYMENTS_API_TOKEN`), [ADR-053](ADR-053-cloudpayments-webhook-user-resolution-via-auth-devices.md) (двухступенчатый резолв deviceId→userId — **не трогается**), [ADR-005](ADR-005-idempotency-ledger.md) (идемпотентность ledger), [ADR-015](ADR-015-consumable-token-iap.md) (anti-tamper сумм из серверных карт), [ADR-018](ADR-018-embedded-auth-issuer.md) (per-IP rate-limit публичных эндпоинтов, образец `enforce_auth_limits`), [ADR-033](ADR-033-llm-provider-abstraction.md) (образец исходящего httpx-клиента). Модуль [billing-cloudpayments](../modules/billing-cloudpayments/README.md).

## Context

**Диагностика ([ADR-052 §3](ADR-052-cloudpayments-webhook-lenient-auth-header.md), прод avelyra).** Диагностический лог показал: **колбэк broadapps приходит БЕЗ авторизации** (`authScheme=none`; ни `Authorization`, ни `X-Api-Key`/`X-Signature`/HMAC). Терпимый разбор ([ADR-052](ADR-052-cloudpayments-webhook-lenient-auth-header.md)) бессилен — авторизующего заголовка **нет вовсе** → constant-time сравнение с `CLOUDPAYMENTS_WEBHOOK_TOKEN` всегда mismatch → **`401`**, начисления нет.

**Корневое заблуждение (уточнено заказчиком).** Api-ключ «для внешних запросов» (значение `e2aXC…`, у нас в `CLOUDPAYMENTS_API_TOKEN`) — ключ для **НАШИХ исходящих вызовов К broadapps**, а **НЕ** для аутентификации входящего колбэка. broadapps **не подписывает и не авторизует** колбэк. Следствия:
- Требовать токен на входе (`401`) — тупик: broadapps его никогда не пришлёт.
- Начислять **по телу колбэка вслепую** нельзя: тело не подписано и подделываемо.

**Единственный доверенный сигнал «оплата состоялась» — broadapps API**, к которому мы обращаемся НАШИМ ключом. Колбэк = **триггер** («проверь платежи этого устройства»), начисление — **только после верификации через broadapps API**.

**Реальные ответы broadapps (подтверждены заказчиком).** Авторизация обоих — `Authorization: Bearer <CLOUDPAYMENTS_API_TOKEN>`, база `CLOUDPAYMENTS_API_BASE=https://pay.broadapps.dev/api/v1`.

`GET /users/{user_id}/payments` → `200`:
```json
{"user_id":"<deviceId>","count":1,"data":[
  {"payment_id":"316c1f83-000f-5000-b000-125e3ba348d1","provider":"...","status":"succeeded",
   "amount":"799.00","currency":"RUB","is_recurring":false,"is_tester":false,
   "paid_at":"2026-04-11T08:52:46+00:00",
   "product":{"code":"100_tokens_9.99","name":"...","payment_type":"one_time"},
   "subscription_id":null}]}
```
`{user_id}` = **deviceId** (id, который iOS передал broadapps = наш `AccountId`/`Data.user_id` из колбэка).

`GET /users/{user_id}/subscriptions` → `200` `{user_id, count, data:[{subscription_id, provider, status, billing_phase, product{code,...}, ...}]}` (для будущего; в гейт начисления MVP **не входит**, §7).

**Ключевые факты, определяющие дизайн (из реальных ответов):**
1. **`status`=«оплачено» = `"succeeded"`** (точное значение). Дефолт `CLOUDPAYMENTS_PAID_STATUSES = {"succeeded"}` (опц. расширяемо `paid`/`completed`/`confirmed`).
2. **`product.code`** — код продукта (в проде = наши коды `100_tokens_9.99` / `week_6.99_nottrial` и т.п.). Кредиты маппятся по **`product.code`** через наши серверные карты (`TOKEN_PRODUCTS` / `CLOUDPAYMENTS_PRODUCT_TOKENS`). (Debug-примеры из тест-приложения `adapty_test_2` игнорируются.)
3. **`product.payment_type ∈ {"subscription","one_time"}`** — авторитетно различает подписку/токены (чище, чем паттерн имени [ADR-050 §3](ADR-050-cloudpayments-webhook.md)): `one_time`→разовый грант токенов; `subscription`→активация подписки + кредиты периода.
4. **Идемпотентность гранта = `cp-txn:{payment_id}`** — стабильный broadapps `payment_id` из ответа `/payments`, **НЕ** callback `TransactionId` (в реальном колбэке был только `TransactionId`/`SubscriptionId`, broadapps `payment_id` в теле **отсутствует** и не обязан совпадать с `TransactionId`).
5. **Колбэк ненадёжно несёт broadapps `payment_id`** → матч «колбэк→конкретный платёж» ненадёжен. Робастный подход: колбэк = триггер реконсиляции — получить `/payments`, отобрать `status=="succeeded"` в **окне свежести**, начислить **каждый ещё не начисленный** `payment_id` (идемпотентно `cp-txn:{payment_id}`). Окно свежести отсекает риск «у юзера 7 старых succeeded-платежей → первый колбэк начислит все разом».

## Decision

Входящий CloudPayments-вебхук становится **публично вызываемым** (без блокирующей авторизации), а **единственным триггером начисления** — **реконсиляция платежей через broadapps API** (`GET /users/{deviceId}/payments`) нашим `CLOUDPAYMENTS_API_TOKEN`: начисляем каждый подтверждённо-`succeeded` платёж в окне свежести, идемпотентно по broadapps `payment_id`. Скоуп — **только** CloudPayments-вебхук + новый broadapps-verify-клиент. Adapty / StoreKit / checkout ([ADR-051](ADR-051-cloudpayments-checkout-payment-link.md)) / миграции / anti-tamper / резолв deviceId→userId ([ADR-053](ADR-053-cloudpayments-webhook-user-resolution-via-auth-devices.md)) — **не трогаются**.

### 1. Вебхук публичный — авторизация НЕ блокирует (снятие `401`)

`require_cloudpayments_webhook` ([ADR-052](ADR-052-cloudpayments-webhook-lenient-auth-header.md)) **перестаёт выдавать `401`**. broadapps шлёт колбэк без авторизации → любое требование токена = вечный `401` = потерянные платежи.

- **Приём без токена.** Отсутствие/несовпадение `Authorization` **не отбивается**. (Обратная совместимость [ADR-052](ADR-052-cloudpayments-webhook-lenient-auth-header.md): токен, если **присутствует и совпадает**, тоже принимается; наличие не требуется и не вредит.)
- **Зависимость сохраняется как наблюдательная (non-blocking).** Читает сырой `Authorization`, вычисляет `matched` (constant-time с `CLOUDPAYMENTS_WEBHOOK_TOKEN`, **только для лога**, если секрет задан) и `authScheme`; **всегда пропускает** (никогда не `raise`). Diagnostic-лог `"cloudpayments_webhook_auth_denied"` (привязанный к 401) **переименован** в `"cloudpayments_webhook_auth_observed"` (DEBUG/INFO) — видимость на случай, если broadapps позже введёт подпись/иной заголовок.
- **Ключевой инвариант безопасности:** **НИ ОДНО начисление без ПОДТВЕРЖДЁННОГО через broadapps API `succeeded`-платежа** (§2). Публичный колбэк не может начислить: нужен резолвимый deviceId (наша `auth_devices`) + авторитетное `succeeded` от broadapps на `GET` нашим ключом + идемпотентность.
- **Rate-limit (обязателен — эндпоинт публичный).** `enforce_cloudpayments_webhook_limits(ip=client_ip(request))` — per-source-IP (образец `enforce_auth_limits` [ADR-018](ADR-018-embedded-auth-issuer.md): fail-open при недоступности Redis, бакет `unknown` при неопределимом IP). Превышение → **`429`**. Лимит щедрый (дефолт `cloudpayments_webhook_rate_limit_per_ip = 120`/мин): легитимные колбэки/ретраи не задевает; цель — не дать флудом раскачать исходящие `GET` (анти-амплификация). `client_ip(request)` (`deps.py`) резолвит IP за доверенным Traefik.
- **Гейт активации по инстансу переезжает на `CLOUDPAYMENTS_API_TOKEN`.** Верификация невозможна без него: `CLOUDPAYMENTS_API_TOKEN == ""` → **`500`** (`CloudPaymentsWebhookMisconfiguredError`, code `cloudpayments_webhook_misconfigured`, «cloudpayments api token not configured»), в начале `handle()` до парсинга. ⇒ вебхук **начисляет только на avelyra** (где задан `API_TOKEN`), как checkout. Прежний гейт по пустому `CLOUDPAYMENTS_WEBHOOK_TOKEN` **снят**: `CLOUDPAYMENTS_WEBHOOK_TOKEN` — **легаси/опционален** (не требуется, не гейтит; только `matched` в наблюдательном логе).

### 2. Поток: колбэк-триггер → реконсиляция платежей (единственный триггер начисления)

`CloudPaymentsWebhookService.handle(raw)` — порядок (ранний `ignored` до исходящего `GET`, чтобы фейковый колбэк не использовал broadapps-API как оракул/амплификатор):

1. **Конфиг-гейт:** `CLOUDPAYMENTS_API_TOKEN` пуст → `500` misconfigured (§1).
2. **Rate-limit** (в роутере, до `handle`) → `429` (§1).
3. **Парсинг колбэка** (парсер [ADR-050 §2](ADR-050-cloudpayments-webhook.md), **упрощён**): empty/json/object → гейт `Status=="Completed"` & `OperationType=="Payment"` (ci) → `X` ← `AccountId`→`Data.user_id` (lower, `uuid.UUID`; не-UUID → `invalid_account_id`). `X` — это **deviceId**. **`TransactionId`/`product_id`/`billing_*` из колбэка — теперь ТОЛЬКО контекст для лога** (не требуются, не гейтят, не участвуют в идемпотентности/начислении): роль колбэка — «случилось событие для этого deviceId → реконсилируй». (Изменение против [ADR-050 §2](ADR-050-cloudpayments-webhook.md): `missing_transaction_id`/`missing_product_id` больше не отсекают — источник истины по продукту/сумме/идемпотентности — ответ `/payments`, не тело.)
4. **Резолв `X`→`userId`** (двухступенчатый, [ADR-053](ADR-053-cloudpayments-webhook-user-resolution-via-auth-devices.md), **без изменений**): (a) `X∈users`→`userId=X`; (b) иначе `X∈auth_devices.device_id`→связанный `user_id`; (c) иначе → **`ignored/user_not_found`** (`200`, WARNING; **без** `GET` — не верифицируем платежи неизвестного пользователя; отсекает фейковые deviceId дёшево).
5. **ВЕРИФИКАЦИЯ (исходящий `GET`):** `CloudPaymentsVerifyClient.list_payments(device_id=X)` → `GET {CLOUDPAYMENTS_API_BASE}/users/{str(X)}/payments`, `Authorization: Bearer {CLOUDPAYMENTS_API_TOKEN}`, `Accept: application/json`, таймаут `_VERIFY_TIMEOUT_SECONDS = 15.0`. Исход:
   - **2xx + валидный JSON с `data`** → шаг 6.
   - **`404`** = «у пользователя нет платежей» (**перманентно**, не транзиентно) → трактовать как **пустой `data`** → шаг 6 даст `no_creditable_payment` (`200`), **НЕ** `500`-retry (иначе неизвестный/фейковый для broadapps deviceId вызвал бы вечные ретраи+GET). Точное поведение `404` — сверить живьём ([Q-054-2](../99-open-questions.md)).
   - **`api_error`** (таймаут / connect / **`5xx`** / не-JSON / нет ключа `data`) → лог outcome `verify=api_error` (WARNING) → `raise CloudPaymentsVerificationUnavailableError` → **`500` (РЕТРАИБЕЛЬНО)**. broadapps перешлёт колбэк позже; **начисления НЕТ**. (Транзиентная ошибка не должна молча ронять платёж; `404` ≠ транзиентная.)
6. **Реконсиляция (чистая функция `select_creditable_payments`):** из `data[]` отобрать платежи, удовлетворяющие ВСЕМ:
   - `str(p["status"]).strip().lower() ∈ CLOUDPAYMENTS_PAID_STATUSES` (дефолт `{"succeeded"}`);
   - **окно свежести:** `paid_at >= now() - CLOUDPAYMENTS_PAYMENT_FRESHNESS_HOURS` (дефолт `72`ч) — отсекает древнюю историю (см. §Окно свежести);
   - валидный `payment_id` и `product.code`.
   Пустой отбор (2xx, но нет свежих `succeeded`) → **`ignored/no_creditable_payment`** (`200`, WARNING; лог фактических `status`/`paid_at` для [Q-054-1](../99-open-questions.md)). **Начисления НЕТ.**
7. **Начисление по каждому отобранному платежу (идемпотентно, отдельная транзакция на платёж):**
   - **Классификация по `product.payment_type`:** `"one_time"` → **tokens**; `"subscription"` → **subscription**; иное/пусто → `unknown` → пропустить платёж (лог `unknown_payment_type`, WARNING; без начисления).
   - **Сумма кредитов — ТОЛЬКО из серверных карт по `product.code`** (anti-tamper [ADR-015](ADR-015-consumable-token-iap.md), **не** из ответа verify/тела):
     - tokens: `credits = settings.token_products().get(product.code)`; `None`/`<=0` → пропустить платёж (`unknown_product`, WARNING).
     - subscription: `credits = settings.cloudpayments_product_tokens().get(product.code) or settings.cloudpayments_subscription_tokens_grant`.
   - **Идемпотентность — `cp-txn:{payment_id}`** (§3): `BEGIN` → dedup INSERT `cloudpayments_webhook_events ON CONFLICT DO NOTHING RETURNING` (ключ = `payment_id`, §3) — пусто → уже начислено, пропустить (**этот payment — `duplicate`**); вставлено → грант `WalletService.grant(user_id=<resolved>, amount=credits, idempotency_key=f"cp-txn:{payment_id}", ...)`; для subscription — upsert `subscriptions ON CONFLICT(user_id)` (active/`plan=product.code`/`expires_at`=`now()`+интервал, §Expiry); audit `cloudpayments_payment`; `COMMIT`. Сбой БД → ROLLBACK → `500` → ретрай.
8. **Агрегатный исход колбэка:** ≥1 платёж начислен → **`applied`** (в лог — счётчик `creditedCount`); все отобранные оказались `duplicate` → **`duplicate`**; отбор пуст → `no_creditable_payment` (§6). Ответ всегда `200 {"code":0}` (кроме `500` api_error/misconfig/DB).

Исходящий `GET` — **вне** открытой БД-транзакции; каждый платёж начисляется в **своей** короткой транзакции (изоляция идемпотентности по платежу). Всё начисление — на **резолвнутый** `userId` (не deviceId).

### §Окно свежести (`CLOUDPAYMENTS_PAYMENT_FRESHNESS_HOURS`, дефолт 72ч)

Реконсиляция берёт только платежи с `paid_at >= now() - freshness`. **Зачем:** идемпотентность `cp-txn:{payment_id}` уже гарантирует «каждый платёж начислим ≤1 раза за всё время», но на **первом** колбэке для пользователя с **предсуществующей** историей (напр. 7 старых `succeeded`) без окна мы начислили бы все разом. Окно ограничивает начисление недавними платежами (триггернувший колбэк платёж свежий — минуты). Референс — `now()` (время приёма, не манипулируемый `paid_at`/`DateTime` из тела). Дефолт `72`ч: щедро для задержек/ретраев доставки колбэка, узко для отсечения старой истории. **Риск:** если первый успешный реконсил платежа случится позже окна (broadapps API лежал > 72ч) — платёж выпадет; митигация — конфигурируемо + [Q-054-2](../99-open-questions.md). `is_tester`/`is_recurring` — логируются, на MVP **не** гейтят.

**Операторский шаг восстановления застрявших платежей (разово, на раскатке).** Платежи, «застрявшие» за инцидентный период (когда вебхук отбивал `401`, ADR-052), лежат вне 72-часового окна. Для их начисления оператор **временно** поднимает `CLOUDPAYMENTS_PAYMENT_FRESHNESS_HOURS` до величины, покрывающей инцидентный период (напр. `2160`=90д), прогоняет ретраи/тестовые колбэки по оплатившим deviceId, затем **возвращает `72`**. Идемпотентность по `payment_id` исключает двойное начисление при повышенном окне (безопасно). См. [module/07-implementation-phases §Deployment](../modules/billing-cloudpayments/07-implementation-phases.md) и [07-deployment.md](../07-deployment.md).

### 3. Идемпотентность — единый ключ по broadapps `payment_id`

Единый ключ и для дедупа события, и для идемпотентности гранта — **broadapps `payment_id`** (стабильный id платежа из `/payments`):
- **Дедуп события:** `cloudpayments_webhook_events` — ключ дедупа = `payment_id`, хранится в существующей UNIQUE-колонке `transaction_id` (**репурпозинг без миграции**: колонка generic-text, прод-таблица пуста от успешных строк; под ADR-054 значение = broadapps `payment_id`, не callback `TransactionId`). `INSERT ... ON CONFLICT (transaction_id) DO NOTHING RETURNING`; пусто → платёж уже начислён. Переименование колонки в `payment_id` — отложенный не-блокирующий долг ([Q-054-3](../99-open-questions.md)).
- **Идемпотентность гранта:** ledger `idempotency_key = f"cp-txn:{payment_id}"` (UNIQUE, [ADR-005](ADR-005-idempotency-ledger.md); namespace `cp-txn:` сохранён, значение = `payment_id`). Изолирован от `adapty-txn:*`/`sub-grant:*`/token-purchase.
- **Почему `payment_id`, а не callback `TransactionId`:** broadapps `payment_id` — авторитетный стабильный id платежа из верификации; callback `TransactionId` (а) может отсутствовать/не совпадать с `payment_id`, (б) при реконсиляции одного колбэка мы начисляем **несколько** платежей — ключ обязан быть per-payment. `TransactionId` — только контекст лога.

### 4. Конфиг

- **Переиспользуются** (уже есть): `CLOUDPAYMENTS_API_BASE` ([ADR-051 §5](ADR-051-cloudpayments-checkout-payment-link.md)), `CLOUDPAYMENTS_API_TOKEN` (секрет; теперь и для верификации вебхука, и для checkout — одна роль «мы→broadapps»); `TOKEN_PRODUCTS` / `CLOUDPAYMENTS_PRODUCT_TOKENS` / `CLOUDPAYMENTS_SUBSCRIPTION_TOKENS_GRANT` (карты кредитов, теперь ключуются `product.code` из verify).
- **Новый** `cloudpayments_paid_statuses: frozenset[str]` (alias `CLOUDPAYMENTS_PAID_STATUSES`, CSV/JSON; дефолт `succeeded`; сравнение lower-case). Фактический `status` логируется на каждом реконсиле для калибровки ([Q-054-1](../99-open-questions.md)).
- **Новый** `cloudpayments_payment_freshness_hours: int` (alias `CLOUDPAYMENTS_PAYMENT_FRESHNESS_HOURS`, дефолт `72`) — окно свежести (§Окно свежести).
- **Новый** `cloudpayments_webhook_rate_limit_per_ip: int` (alias `CLOUDPAYMENTS_WEBHOOK_RATE_LIMIT_PER_IP`, дефолт `120`) — per-IP лимит публичного вебхука (§1).
- `CLOUDPAYMENTS_WEBHOOK_TOKEN` — **больше не гейтит** (легаси/опционально).

### 5. Наблюдаемость (образец [ADR-046](ADR-046-adapty-webhook-outcome-logging.md))

- `"cloudpayments_webhook_outcome"` ([ADR-050 §7](ADR-050-cloudpayments-webhook.md)/[ADR-053](ADR-053-cloudpayments-webhook-user-resolution-via-auth-devices.md)) **дополняется** полями:
  - **`verify`** ∈ `"ok"` | `"api_error"` — результат исходящего `GET`;
  - **`creditedCount`** — сколько платежей начислено на этом колбэке (0 при `no_creditable_payment`/`duplicate`);
  - **`paymentStatuses`** — список фактических broadapps `status` из `data[]` (для [Q-054-1](../99-open-questions.md); безопасно — не PII, не секрет);
  - **`resolvedVia`** — из [ADR-053](ADR-053-cloudpayments-webhook-user-resolution-via-auth-devices.md) (`user_id`/`device_id`).
  - На до-verify исходах (`user_not_found`) поля `verify`/`creditedCount`/`paymentStatuses` опущены.
- Уровни: `no_creditable_payment`/`unknown_product`/`unknown_payment_type` → **WARNING**; `api_error` → **WARNING** (перед 500); `applied`/`duplicate` → INFO. **Ровно один** агрегатный `outcome`-лог на колбэк (плюс, опц., DEBUG per-payment `cloudpayments_payment_credited`).
- Наблюдательный `"cloudpayments_webhook_auth_observed"` (§1) — отдельная DEBUG/INFO запись в `auth.py` (allowlist `matched`/`authScheme`/`presentAuthHeaders`, [ADR-052 §3](ADR-052-cloudpayments-webhook-lenient-auth-header.md)).
- **ЗАПРЕЩЕНО:** `CLOUDPAYMENTS_API_TOKEN`/Bearer, карт-данные, сырой payload/`Data`, `customer_email`, `amount`/`currency`, полное тело ответа verify.

### 6. Безопасность

- **Верификация — trust-anchor.** Начисление только по подтверждённому broadapps `succeeded`-платежу. Публичный колбэк не может начислить: нужен (1) `X`=deviceId, резолвимый через **нашу** `auth_devices`; (2) авторитетное `succeeded` от broadapps на `GET` нашим ключом (не подделать); (3) уникальный `payment_id` (не задвоить); (4) окно свежести. Форжед-колбэк → максимум бесполезный `GET` → `no_creditable_payment`. Rate-limit ограничивает амплификацию `GET`.
- **`CLOUDPAYMENTS_API_TOKEN`** — секрет: не логируется, не в ответе, только `Bearer` к **фиксированному** хосту `CLOUDPAYMENTS_API_BASE`. `authorization` в redaction-денилисте.
- **SSRF-защита `GET`.** `{user_id}` в пути = `X`, провалидированный как `uuid.UUID` (не-UUID отсекается `invalid_account_id` **до** verify) → `str(X)` каноничен, без инъекции из тела; хост — из config.
- **Anti-tamper.** Сумма — по `product.code` из серверных карт, не из ответа verify/тела. `payment_type` — из авторитетного broadapps-ответа.
- **PII.** `customer_email` не участвует; карт-данные не читаются/не хранятся.

### 7. Скоуп и что НЕ трогается

- **Трогаем:** `billing_cloudpayments/auth.py` (снять `401`, наблюдательный лог), `service.py` (реконсиляция, конфиг-гейт по `API_TOKEN`, новые исходы), **новый** `verify.py` (`CloudPaymentsVerifyClient.list_payments` + `select_creditable_payments` + `CreditablePayment`), `parser.py` (упростить: `X`+гейт обязательны, `TransactionId`/`product_id` → опц. контекст), `config.py` (3 новых поля), `api_gateway/rate_limit.py` (`enforce_cloudpayments_webhook_limits`), `routers/billing_cloudpayments.py` (rate-limit), `errors.py` (`CloudPaymentsVerificationUnavailableError` 500), `deps.py` (фабрика verify-клиента).
- **НЕ трогаем:** checkout ([ADR-051](ADR-051-cloudpayments-checkout-payment-link.md)) — **переиспользуем** конфиги/httpx-паттерн (контракт `checkout.py` не меняем); Adapty; StoreKit; **миграции** (`cloudpayments_webhook_events`/ledger/subscriptions уже есть; колонка `transaction_id` репурпозится под `payment_id` без DDL); anti-tamper; резолв deviceId→userId ([ADR-053](ADR-053-cloudpayments-webhook-user-resolution-via-auth-devices.md)); HTTP-семантика `{"code":0}` (кроме `500`).
- **`classify_product`** ([ADR-050 §3](ADR-050-cloudpayments-webhook.md)) в вебхуке-начислении **больше не используется** (классификация теперь по авторитетному `product.payment_type`); остаётся для checkout-allowlist ([ADR-051 §2](ADR-051-cloudpayments-checkout-payment-link.md)).
- **`GET /users/{id}/subscriptions`** — вне гейта начисления MVP (авторитетный «оплачено» даёт `/payments`); доступен для будущего (lifecycle/отмена) отдельным ADR.

## Consequences

**Плюсы.**
- Закрыт корневой инцидент: колбэк без авторизации → `401` устранён (публичный эндпоинт), начисление защищено **верификацией через broadapps API** по авторитетным полям (`succeeded`/`product.code`/`payment_type`).
- Trust-anchor — «авторитетное подтверждение broadapps нашим ключом», устойчив к подделке колбэка.
- Классификация по `product.payment_type` и сумма по `product.code` — точнее и проще паттерн-эвристики [ADR-050 §3](ADR-050-cloudpayments-webhook.md).
- Идемпотентность по стабильному `payment_id`; реконсиляция начисляет любой недоначисленный `succeeded`-платёж (устойчиво к ненадёжному матчу колбэк→платёж и к пропущенным доставкам).
- Транзиентная недоступность broadapps → `500`→ретрай (платёж не теряется).
- Без миграций; anti-tamper/резолв не тронуты.

**Минусы / риски / долг.**
- Окно свежести — эвристический компромисс ([Q-054-2](../99-open-questions.md)): слишком узкое → пропуск при долгой недоступности broadapps; слишком широкое → риск начисления старой истории на первом колбэке. Дефолт 72ч, конфигурируемо.
- Точный набор `succeeded` — допущение с запасом ([Q-054-1](../99-open-questions.md)); фактический `status` логируется, правка через env.
- Публичный эндпоинт → поверхность абуза; митигация: `user_not_found` без `GET`, per-IP rate-limit, идемпотентность.
- Колонка `transaction_id` репурпозится под `payment_id` (семантический долг, [Q-054-3](../99-open-questions.md)); переименование — отдельной миграцией при желании.
- +1 исходящий `GET` на колбэк; expiry подписки — timedelta-приближение по интервалу из `product.code` (не календарно-точно; наследует [Q-050-3](../99-open-questions.md)).

## Alternatives (отвергнуто)

- **Начислять по телу колбэка вслепую (ADR-050 до верификации).** Отвергнуто: тело не подписано → подделываемо (чужой deviceId + `Status=Completed` → кража начислений).
- **Оставить `401` при отсутствии токена (ADR-052).** Отвергнуто: `authScheme=none` → `401` вечен → потерянные платежи.
- **Аутентифицировать колбэк через `CLOUDPAYMENTS_API_TOKEN`.** Отвергнуто: это ключ НАШИХ исходящих вызовов, broadapps его в колбэке не шлёт.
- **Матч по одному платежу (payment_id из тела / продукт+свежесть).** Отвергнуто: колбэк ненадёжно несёт broadapps `payment_id`; матч «один колбэк→один платёж» хрупок. Реконсиляция всех недоначисленных `succeeded` в окне — робастнее и самовосстанавливается при пропущенных доставках.
- **Идемпотентность по callback `TransactionId`.** Отвергнуто: `TransactionId` ≠ broadapps `payment_id`, может отсутствовать; при реконсиляции нескольких платежей нужен per-payment ключ. `cp-txn:{payment_id}` стабилен и корректен.
- **Классификация паттерном имени продукта ([ADR-050 §3](ADR-050-cloudpayments-webhook.md)).** Заменено на авторитетный `product.payment_type` — точнее, без эвристик.
- **Без окна свежести (только идемпотентность).** Отвергнуто: первый колбэк для юзера с историей начислил бы все старые `succeeded` разом.
- **`api_error → 200`-ignored.** Отвергнуто: транзиентная недоступность молча уронила бы платёж; `500`→ретрай безопаснее (фейковые deviceId отсекаются `user_not_found` до verify → 5xx не размножаются на мусоре).
- **Верифицировать через `/subscriptions` (или оба).** Отвергнуто для MVP-гейта: `/payments` даёт авторитетный «оплачено» на любой платёж (в т.ч. token/разовый); `/subscriptions` — lifecycle, на будущее.
- **Убрать зависимость `require_cloudpayments_webhook`.** Отвергнуто: пропадёт наблюдательный auth-лог и Swagger security-схема. Оставляем наблюдательной non-blocking.
