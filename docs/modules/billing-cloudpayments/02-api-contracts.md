# billing-cloudpayments / 02 — API Contracts

Модуль содержит **две половины** RU-контура:
- **Исходящая** — `POST /v1/billing/cloudpayments/checkout` ([ADR-051](../../adr/ADR-051-cloudpayments-checkout-payment-link.md)): **наш** JWT-эндпоинт создаёт платёжную ссылку через broadapps.
- **Входящая** — `POST /v1/billing/cloudpayments/webhook` ([ADR-050](../../adr/ADR-050-cloudpayments-webhook.md)): broadapps присылает колбэк о состоявшейся оплате.

## POST /v1/billing/cloudpayments/checkout

**Наш** эндпоинт создания платёжной ссылки RU-оплаты. **Вызывает iOS-клиент** (JWT). Делает один исходящий вызов broadapps `POST /payments/link` и возвращает `paymentUrl` (ссылка YooKassa). Контракт целиком — [ADR-051](../../adr/ADR-051-cloudpayments-checkout-payment-link.md). Активен **только на avelyra** (где заданы `CLOUDPAYMENTS_APP_ID`+`CLOUDPAYMENTS_API_TOKEN`).

### Авторизация
- Пользовательский **JWT** (`Authorization: Bearer <JWT>`, `bearerAuth`, `CurrentUser`) — как прочие `/v1/*`. Нет/невалидный → `401`.
- **`userId` берётся из JWT `sub`, НЕ из тела** — ключевая мера (устраняет клиент-контролируемый `user_id`, из-за которого платежи «терялись»). Тело **не содержит** `userId`/`appId`.
- Rate-limit `enforce_other_limits(user_id=sub)` → `429`.

### Тело запроса (`CloudPaymentsCheckoutRequest`, StrictModel `extra="forbid"`)

```json
{ "productId": "week_6.99_nottrial", "customerEmail": "user@example.com" }
```

| Поле | Тип | Обязательно | Правило |
|---|---|---|---|
| `productId` | str | да | непустой; валидируется allowlist-предикатом `classify_product` (см. ниже). Неизвестный/некредитуемый → `422` |
| `customerEmail` | EmailStr | да | email покупателя (broadapps требует). Невалидный → `422` |

**Валидация `productId`** (симметрия с вебхуком, [03-architecture §Валидация productId](03-architecture.md)): `classify_product(productId, billing_interval_unit=None, frozenset(token_products()))`; `unknown` → `422`; `tokens` с `token_products().get(productId,0)<=0` → `422`. `productId` НЕ определяет сумму гранта (только allowlist-гейт).

### Ответ (`CloudPaymentsCheckoutResponse`, StrictModel) — проброс полей broadapps

```json
{ "paymentId": "e3d7ffe4-...", "paymentUrl": "https://yoomoney.ru/checkout/payments/v2/contract?orderId=...", "status": "pending", "expiresAt": null }
```

| Поле | Тип | Источник (broadapps) |
|---|---|---|
| `paymentId` | str | `payment_id` |
| `paymentUrl` | str | `payment_url` (ссылка YooKassa; тип `str`, не `HttpUrl` — passthrough) |
| `status` | str | `status` |
| `expiresAt` | str \| null | `expires_at` (nullable, passthrough без парсинга) |

### Коды ответа

| HTTP | Код | Когда |
|---|---|---|
| 200 | — | успех: broadapps вернул ссылку (`result=created`) |
| 401 | `unauthorized` | нет/невалидный JWT |
| 422 | `validation_error` | неизвестный/некредитуемый `productId`, невалидный `customerEmail`, лишнее поле |
| 429 | `rate_limited` | превышен лимит |
| 502 | `upstream_error` | broadapps недоступен/таймаут/не-2xx/malformed-ответ (без утечки деталей/токена) |
| 503 | `cloudpayments_checkout_not_configured` | `CLOUDPAYMENTS_APP_ID`/`CLOUDPAYMENTS_API_TOKEN` не заданы (⇒ активен только на avelyra) |

### Исходящий вызов broadapps
- `POST {CLOUDPAYMENTS_API_BASE}/payments/link` (default base `https://pay.broadapps.dev/api/v1`), **multipart/form-data** {`app_id`(config), `product_id`, `user_id`(=JWT `sub`), `customer_email`}, `Authorization: Bearer <CLOUDPAYMENTS_API_TOKEN>`, `Accept: application/json`, таймаут 15с. Детали — [03-architecture §Исходящий вызов](03-architecture.md).

> **Контракт исходящего вызова — сверить живьём ([Q-051-1](../../99-open-questions.md)):** имена multipart-полей и shape `201`-ответа взяты из спеки заказчика; после деплоя прислать тестовый checkout и убедиться, что broadapps вернул `payment_url`.

---

## POST /v1/billing/cloudpayments/webhook

Серверный вебхук агрегатора **broadapps** в формате **CloudPayments**. **Вызывает broadapps**, не iOS-клиент. Базовый контракт — [ADR-050](../../adr/ADR-050-cloudpayments-webhook.md); **АКТУАЛЬНОЕ поведение авторизации и начисления — [ADR-054](../../adr/ADR-054-cloudpayments-webhook-payment-verification.md)** (пересматривает [ADR-050 §1..§6](../../adr/ADR-050-cloudpayments-webhook.md), отменяет 401 из [ADR-052](../../adr/ADR-052-cloudpayments-webhook-lenient-auth-header.md)).

> **[ADR-054](../../adr/ADR-054-cloudpayments-webhook-payment-verification.md): колбэк = ТРИГГЕР, а не источник начисления.** broadapps шлёт колбэк **без авторизации** и **без подписи** → начислять по телу нельзя. Начисление — **только после реконсиляции платежей через broadapps API** (`GET /users/{deviceId}/payments`) нашим `CLOUDPAYMENTS_API_TOKEN`: отбираются `status=="succeeded"` в окне свежести, начисляется каждый ещё не начисленный `payment_id`.

### Авторизация ([ADR-054](../../adr/ADR-054-cloudpayments-webhook-payment-verification.md) — публичный, non-blocking)
- **Эндпоинт публичный: `401` НЕ выдаётся.** broadapps шлёт колбэк без `Authorization` (`authScheme=none`, диагностика [ADR-052 §3](../../adr/ADR-052-cloudpayments-webhook-lenient-auth-header.md)); блокирующий токен = вечный `401` = потерянные платежи. Отсутствие/несовпадение токена **не отбивается**; присутствующий валидный токен тоже принимается (обратная совместимость, но не требуется).
- **Trust-anchor начисления — верификация через broadapps API**, а не токен: ни одно начисление без подтверждённого `succeeded`-платежа. `require_cloudpayments_webhook` остаётся **наблюдательной** зависимостью (читает `Authorization`, вычисляет `matched`/`authScheme` только для лога `cloudpayments_webhook_auth_observed`, всегда пропускает).
- **Rate-limit (эндпоинт публичный):** per-source-IP `enforce_cloudpayments_webhook_limits(ip=client_ip(request))` (дефолт `CLOUDPAYMENTS_WEBHOOK_RATE_LIMIT_PER_IP=120`/мин, fail-open при недоступности Redis) → превышение `429`. Анти-амплификация исходящих `GET`.
- **Гейт активации инстанса — `CLOUDPAYMENTS_API_TOKEN`:** пуст → `500` misconfigured (верификация невозможна) ⇒ вебхук начисляет **только на avelyra**. `CLOUDPAYMENTS_WEBHOOK_TOKEN` — легаси/опционален (не гейтит, только `matched` в логе). `CLOUDPAYMENTS_API_TOKEN`/Bearer **не логируются**.
- **OpenAPI:** security-схема `cloudPaymentsWebhook` (`HTTPBearer`) сохраняется декоративно (замок в Swagger), реальной проверки токена нет.

> **Исторически (отменено [ADR-054](../../adr/ADR-054-cloudpayments-webhook-payment-verification.md)):** [ADR-050 §1](../../adr/ADR-050-cloudpayments-webhook.md) требовал `Authorization: Bearer <CLOUDPAYMENTS_WEBHOOK_TOKEN>` (constant-time, `401` на mismatch); [ADR-052](../../adr/ADR-052-cloudpayments-webhook-lenient-auth-header.md) сделал разбор терпимым к формату (`Bearer`/`Token`/сырой) + WARNING `cloudpayments_webhook_auth_denied` на 401. Диагностика показала `authScheme=none` → 401-путь снят; лог переименован в `cloudpayments_webhook_auth_observed` (DEBUG/INFO). [Q-052-1](../../99-open-questions.md) закрыт.

### Тело запроса
- **Без схемы / без Pydantic-валидации.** Читается сырое (`await request.body()`). Кривой payload не должен давать `422` (иначе агрегатор может ретраить).
- Плоские поля — **PascalCase**; поля внутри `Data` — **snake_case**. **`Data` — это JSON-строка** (парсится отдельным `json.loads`; дефенсивно принимается и уже-словарь).

#### Ключевые поля (пример — годовая подписка)
```json
{
  "Data": "{\"user_id\":\"B284721F-C3E0-4446-B00F-3C6A21F32535\",\"product_id\":\"yearly_49.99_nottrial\",\"billing_interval_unit\":\"year\",\"billing_interval_count\":\"1\",\"billing_phase\":\"regular\",\"subscription_id\":\"f95b318c-...\",\"is_trial_initial\":false,\"paymentGateway\":\"yookassa\"}",
  "Status": "Completed", "OperationType": "Payment", "TestMode": false,
  "Amount": 3990, "Currency": "RUB",
  "AccountId": "B284721F-C3E0-4446-B00F-3C6A21F32535",
  "TransactionId": "31d884c8-000f-5001-8000-1fb75b44e1d9",
  "SubscriptionId": "f95b318c-...",
  "CardFirstSix": "220024", "CardLastFour": "8808", "Issuer": "VTB", "CardType": "Mir", "Description": "Годовая подписка"
}
```
- `AccountId` (верх) → fallback `Data.user_id` — **идентификатор-кандидат `X`** (приходит в ВЕРХНЕМ регистре → нормализуется к lower, парсится в UUID). На RU-флоу broadapps шлёт сюда **deviceId** (id устройства), а не наш `userId`; резолв `X`→наш `userId` — **двухступенчатый** ([ADR-053](../../adr/ADR-053-cloudpayments-webhook-user-resolution-via-auth-devices.md), см. §Резолв пользователя ниже).
- `TransactionId` (верх) — **опц. контекст лога** ([ADR-054](../../adr/ADR-054-cloudpayments-webhook-payment-verification.md)); дедуп/идемпотентность — по broadapps **`payment_id`** из верифицированного ответа `/payments`, НЕ по `TransactionId`.
- `CardFirstSix`/`CardLastFour`/`Issuer`/`CardType` — **PII, НЕ логируются и НЕ персистятся** ([08-observability](08-observability.md)).

Полный порядок источников/парсинг — [03-architecture.md §Дефенсивный парсинг](03-architecture.md).

#### Резолв пользователя — двухступенчатый (deviceId → userId, [ADR-053](../../adr/ADR-053-cloudpayments-webhook-user-resolution-via-auth-devices.md))
Идентификатор `X` (из `AccountId`→`Data.user_id`, lower/UUID) резолвится в наш `userId` в **две ступени** (первое совпадение выигрывает), детали — [03-architecture.md §Резолв пользователя](03-architecture.md#резолв-пользователя--двухступенчатый-deviceid--userid-adr-053):

| Ступень | Условие | Результат | `resolvedVia` (лог) |
|---|---|---|---|
| (a) | `X` есть в `users` | `userId = X` (уже наш id; обратная совместимость с [ADR-051](../../adr/ADR-051-cloudpayments-checkout-payment-link.md)) | `user_id` |
| (b) | иначе `X` есть в `auth_devices.device_id` | `userId = auth_devices[X].user_id` (deviceId→userId, [ADR-018](../../adr/ADR-018-embedded-auth-issuer.md)) | `device_id` |
| (c) | иначе (нет ни в `users`, ни в `auth_devices`) | `ignored/user_not_found` — `200 {"code":0}` + WARNING, **без** создания пользователя/устройства | — |

**Всё начисление/подписка/дедуп/идемпотентность/audit — на резолвнутый `userId`** (не на deviceId). Идемпотентность — `cp-txn:{payment_id}` (broadapps `payment_id`, [ADR-054](../../adr/ADR-054-cloudpayments-webhook-payment-verification.md); резолв deviceId — вход для верификации `GET /users/{deviceId}/payments`); anti-tamper не меняется. Маппинг deviceId→userId — **только из нашей `auth_devices`** (телу колбэка не доверяем). Скоуп — только этот вебхук (Adapty/checkout не затронуты).

#### Гейт колбэка и реконсиляция ([ADR-054](../../adr/ADR-054-cloudpayments-webhook-payment-verification.md))
- Колбэк обрабатывается только при `Status=="Completed"` (ci) И `OperationType=="Payment"` (ci); иначе `ignored/not_a_completed_payment`. Обязателен резолвимый `X`=deviceId; `TransactionId`/`product_id` из тела — **опц. контекст лога** (не гейтят, не участвуют в начислении).
- **Начисление — по ответу `GET /users/{deviceId}/payments`**, не по телу колбэка: отбираются платежи `status ∈ CLOUDPAYMENTS_PAID_STATUSES` (дефолт `{"succeeded"}`) с `paid_at` в окне свежести (`CLOUDPAYMENTS_PAYMENT_FRESHNESS_HOURS`, дефолт 72ч); по каждому недоначисленному `payment_id` — грант.
- **Классификация — по авторитетному `product.payment_type`** (не паттерн имени): `one_time`→tokens; `subscription`→subscription. Сумма кредитов — по `product.code` из серверных карт (anti-tamper).

#### Маппинг (ADR-054)

| `product.payment_type` | Эффект `subscriptions` | Кредиты (anti-tamper, по `product.code`) |
|---|---|---|
| `subscription` | upsert `active`, `plan=product.code`, `expires_at=now+interval` (unit инферится из `code`) | `cloudpayments_product_tokens().get(code) or cloudpayments_subscription_tokens_grant`, идемпотентно по `cp-txn:{payment_id}` |
| `one_time` | не трогается | разовый грант `N = token_products().get(code)`; не в карте/≤0 → пропуск платежа (`unknown_product`, WARNING) |
| иное/пусто | — | пропуск платежа (`unknown_payment_type`, WARNING) |

### Ответы ([ADR-054](../../adr/ADR-054-cloudpayments-webhook-payment-verification.md))

Все `200` c телом `{"code": 0}`, кроме `429`/`500`. Внутренний исход (`result`/`reason`) — **в лог/audit**, не в тело.

| HTTP | Тело | Когда (лог `result/reason`) |
|---|---|---|
| 429 | (rate-limited) | превышен per-IP лимит вебхука (`CLOUDPAYMENTS_WEBHOOK_RATE_LIMIT_PER_IP`) |
| 500 | (мис-конфигурация) | `CLOUDPAYMENTS_API_TOKEN` не задан (верификация невозможна ⇒ активен только avelyra) |
| 500 | (retriable) | **`api_error`** — broadapps `/payments` недоступен/не-2xx/malformed (лог `verify=api_error`, WARNING) → broadapps перешлёт колбэк; **НЕ начисляем** |
| 500 | (внутренний сбой) | БД недоступна и т. п. → ретрай |
| 200 | `{"code":0}` | `ignored/empty_body` \| `invalid_json` \| `not_an_object` |
| 200 | `{"code":0}` | `ignored/not_a_completed_payment` (Status≠Completed или OperationType≠Payment) |
| 200 | `{"code":0}` | `ignored/invalid_account_id` (нет/не-UUID `AccountId`/`Data.user_id`) |
| 200 | `{"code":0}` | `ignored/user_not_found` (WARNING) — `X` не найден ни в `users`, ни в `auth_devices` ([ADR-053](../../adr/ADR-053-cloudpayments-webhook-user-resolution-via-auth-devices.md)); **без** исходящего GET |
| 200 | `{"code":0}` | `ignored/no_creditable_payment` (WARNING) — 2xx от broadapps, но нет свежих `succeeded`-платежей (лог `paymentStatuses`) |
| 200 | `{"code":0}` | `duplicate` — все отобранные платежи уже начислены |
| 200 | `{"code":0}` | `applied` (≥1 `payment_id` начислен; `creditedCount`) |

> **Публичный эндпоинт (нет `401`, [ADR-054](../../adr/ADR-054-cloudpayments-webhook-payment-verification.md)).** Форжед-колбэк максимум триггерит бесполезный `GET`→`no_creditable_payment`. Формат ответа `{"code":0}` — допущение ([Q-050-1](../../99-open-questions.md)); точный `status`=«оплачено» подтверждён = `succeeded` ([Q-054-1](../../99-open-questions.md)).

### Эффекты при `applied`
- **По каждому свежему `succeeded`-платежу (своя транзакция, идемпотентно `cp-txn:{payment_id}`):** `subscription`→ upsert `subscriptions` (`active`/`plan=product.code`/`expires_at`); `one_time`→ разовый грант `N` из `TOKEN_PRODUCTS[code]`. `creditedCount` = число начисленных платежей.

### Идемпотентность ([ADR-054](../../adr/ADR-054-cloudpayments-webhook-payment-verification.md) — по broadapps `payment_id`)
- **Единый ключ — broadapps `payment_id`** (стабильный id из `/payments`), НЕ callback `TransactionId`.
- **Дедуп события:** `payment_id` в UNIQUE-колонке `cloudpayments_webhook_events.transaction_id` (репурпозинг без миграции, [Q-054-3](../../99-open-questions.md)); `ON CONFLICT DO NOTHING RETURNING` → уже начислен → пропуск.
- **Идемпотентность гранта:** ledger `idempotency_key = cp-txn:{payment_id}` (UNIQUE, [ADR-005](../../adr/ADR-005-idempotency-ledger.md)); namespace изолирован. Один грант на `payment_id`; продление — новый `payment_id` → новый грант.
