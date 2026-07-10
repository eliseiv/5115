# billing-cloudpayments / 03 — Architecture

Реализует [ADR-050](../../adr/ADR-050-cloudpayments-webhook.md) (входящий вебхук) и [ADR-051](../../adr/ADR-051-cloudpayments-checkout-payment-link.md) (исходящий checkout). Ниже — точные детали для backend (без додумывания). Образец структуры — модуль [billing-adapty](../billing-adapty/README.md).

## Checkout — исходящий вызов broadapps ([ADR-051](../../adr/ADR-051-cloudpayments-checkout-payment-link.md))

### Файлы (checkout)
- `src/app/billing_cloudpayments/checkout.py` — **новый**: `CloudPaymentsCheckoutClient` (исходящий httpx-вызов + маппинг ошибок) + `CheckoutResult` (dataclass: `payment_id`, `payment_url`, `status`, `expires_at`) + `_CHECKOUT_TIMEOUT_SECONDS = 15.0`. Метод `async def create_payment_link(*, user_id: uuid.UUID, product_id: str, customer_email: str) -> CheckoutResult`. Валидацию `productId` (`validate_product` через `parser.classify_product`) держать здесь **или** в роутере — единая точка; сумму гранта НЕ считает.
- `src/app/schemas/billing_cloudpayments.py` — **добавить** `CloudPaymentsCheckoutRequest` (`productId: str`, `customerEmail: EmailStr`, StrictModel) + `CloudPaymentsCheckoutResponse` (`paymentId`, `paymentUrl: str`, `status`, `expiresAt: str | None`). Существующий `CloudPaymentsWebhookResponse` не трогать.
- `src/app/api_gateway/routers/billing_cloudpayments.py` — **добавить** роут `POST /v1/billing/cloudpayments/checkout` в тот же `router` (prefix `/v1/billing/cloudpayments`), но с `CurrentUser` + rate-limit (per-route, изолированно от webhook-bearer).
- `src/app/config.py` — **3 новых поля** (`cloudpayments_api_base`/`cloudpayments_app_id`/`cloudpayments_api_token`) рядом с блоком CloudPayments-вебхука.
- `src/app/deps.py` — фабрика `get_cloudpayments_checkout_client() -> CloudPaymentsCheckoutClient` (нужен только `get_settings()`, **без** DbSession — passthrough без БД).
- `src/app/errors.py` — `CloudPaymentsCheckoutNotConfiguredError(ServiceUnavailableError)` (`status_code=503`, `code="cloudpayments_checkout_not_configured"`). `UpstreamError` (502) переиспользуется.
- `src/app/main.py` — **без изменений** (router уже зарегистрирован; добавляется лишь новый роут в существующий `router`).
- **Миграции — НЕТ** (passthrough).
- **Зависимость:** `EmailStr` требует `email-validator` — добавить в `pyproject.toml` dependencies (напр. `email-validator>=2,<3`) как явную (сейчас присутствует лишь транзитивно).

### Поток checkout
```mermaid
sequenceDiagram
    participant C as iOS (JWT)
    participant R as Router (/checkout)
    participant K as CloudPaymentsCheckoutClient
    participant B as broadapps (/payments/link)
    C->>R: POST + Bearer <JWT> + {productId, customerEmail}
    R->>R: get_current_user → userId=sub (+ lazy provision users)
    R->>R: config gate: app_id&api_token заданы? иначе 503
    R->>R: rate-limit (enforce_other_limits) иначе 429
    R->>R: validate_product(productId) иначе 422
    R->>K: create_payment_link(user_id=sub, product_id, customer_email)
    K->>B: POST multipart {app_id,product_id,user_id,customer_email} + Bearer <api_token>
    alt timeout / connect / не-2xx / malformed
        K-->>R: raise UpstreamError → 502 (без утечки токена/деталей)
    else 201 OK
        B-->>K: {payment_id, payment_url, status, expires_at}
        K-->>R: CheckoutResult
        R-->>C: 200 {paymentId, paymentUrl, status, expiresAt}
    end
    Note over R,K: log "cloudpayments_checkout_outcome" (allowlist; без email/токена)
```

### Исходящий вызов (детали для backend)
- `httpx.AsyncClient` per-call (`async with`), `POST {settings.cloudpayments_api_base}/payments/link`.
- **multipart/form-data** — через `files=` (НЕ `data=`, тот даёт urlencoded):
  ```
  files = {
      "app_id":         (None, settings.cloudpayments_app_id),
      "product_id":     (None, product_id),
      "user_id":        (None, str(user_id)),
      "customer_email": (None, customer_email),
  }
  headers = {"Authorization": f"Bearer {settings.cloudpayments_api_token}", "Accept": "application/json"}
  ```
  **Content-Type руками НЕ ставить** (httpx выставит boundary).
- Таймаут `_CHECKOUT_TIMEOUT_SECONDS = 15.0`.
- Маппинг ошибок → `UpstreamError` (502): `httpx.TimeoutException`→`timeout`; `httpx.RequestError`→`connect_error`; статус не `2xx` (success=`201`; принять `200`/`201`)→`upstream_status`; `2xx` без `payment_url`/не-JSON→`malformed_response`. Наружу — generic 502, **без** upstream-тела/статуса/токена.

### Валидация productId (`validate_product`)
Переиспользует `parser.classify_product` как allowlist-предикат (checkout не знает `billing_interval_unit` — он приходит только в колбэке, поэтому передаётся `None`, классификация по имени):
```
kind = classify_product(product_id, None, frozenset(settings.token_products()))
if kind == KIND_UNKNOWN:                                   raise ValidationFailedError("unknown_product")  # 422
if kind == KIND_TOKENS and settings.token_products().get(product_id, 0) <= 0:  raise ValidationFailedError("unknown_product")  # 422
```
Гарантирует симметрию: checkout выдаёт ссылку только на продукт, который вебхук ([ADR-050](../../adr/ADR-050-cloudpayments-webhook.md)) сможет начислить.

### Наблюдаемость checkout
Ровно один структурный лог `"cloudpayments_checkout_outcome"` на вызов. **Allowlist:** `result` (`created`|`error`), `reason` (на ошибке), `userId` (наш UUID), `productId`, `status` (broadapps-статус на успехе), `paymentId` (на успехе). **ЗАПРЕЩЕНО:** `customer_email` (PII), `CLOUDPAYMENTS_API_TOKEN`/Bearer, `app_id`, upstream-тело. Без persist/audit-строки (log-only, [ADR-051 §6](../../adr/ADR-051-cloudpayments-checkout-payment-link.md)).

---

## Webhook — входящий вызов ([ADR-050](../../adr/ADR-050-cloudpayments-webhook.md), пересмотрен [ADR-054](../../adr/ADR-054-cloudpayments-webhook-payment-verification.md))

> **АКТУАЛЬНАЯ модель ([ADR-054](../../adr/ADR-054-cloudpayments-webhook-payment-verification.md)) — читать первой.** Ниже разделы ADR-050/052/053 описывают исходный дизайн; [ADR-054](../../adr/ADR-054-cloudpayments-webhook-payment-verification.md) **пересматривает** авторизацию (401 снят, эндпоинт публичный), триггер начисления (реконсиляция через broadapps API), классификацию (`product.payment_type`) и идемпотентность (broadapps `payment_id`). При расхождении — приоритет у §Верификация ниже.

## Верификация платежей — единственный триггер начисления ([ADR-054](../../adr/ADR-054-cloudpayments-webhook-payment-verification.md))

**Причина.** broadapps шлёт колбэк **без авторизации** (`authScheme=none`, диагностика [ADR-052 §3](../../adr/ADR-052-cloudpayments-webhook-lenient-auth-header.md)) и **без подписи** — по телу начислять нельзя. `CLOUDPAYMENTS_API_TOKEN` — ключ НАШИХ исходящих вызовов К broadapps. Колбэк = **триггер**; начисление — только после подтверждения `succeeded`-платежа через broadapps API.

### Файлы (ADR-054)
- `src/app/billing_cloudpayments/verify.py` — **новый**: `CloudPaymentsVerifyClient.list_payments(*, device_id: uuid.UUID) -> list[dict]` (исходящий `GET`, маппинг ошибок → `api_error`); чистая `select_creditable_payments(data, *, paid_statuses: frozenset[str], now, freshness_hours: int) -> list[CreditablePayment]`; dataclass `CreditablePayment(payment_id: str, product_code: str, payment_type: str, status: str, paid_at: datetime, ...)`; константа `_VERIFY_TIMEOUT_SECONDS = 15.0`.
- `src/app/billing_cloudpayments/auth.py` — `require_cloudpayments_webhook` **не выдаёт `401`** (наблюдательный non-blocking); лог `cloudpayments_webhook_auth_observed` (DEBUG/INFO); `CloudPaymentsWebhookMisconfiguredError` (500) теперь триггерится пустым **`CLOUDPAYMENTS_API_TOKEN`** (в `handle`, не в auth-dep).
- `src/app/billing_cloudpayments/service.py` — реконсиляция в `handle()` (порядок ниже); интеграция `CloudPaymentsVerifyClient`; агрегатный исход.
- `src/app/billing_cloudpayments/parser.py` — **упростить**: обязательны только `X`(deviceId)+гейт `Status/OperationType`; `TransactionId`/`product_id`/`billing_*` → опц. контекст лога (не гейтят). `classify_product` в начислении **не используется** (остаётся для checkout [ADR-051](../../adr/ADR-051-cloudpayments-checkout-payment-link.md)).
- `src/app/config.py` — **3 новых**: `cloudpayments_paid_statuses` (`CLOUDPAYMENTS_PAID_STATUSES`, дефолт `succeeded`), `cloudpayments_payment_freshness_hours` (`CLOUDPAYMENTS_PAYMENT_FRESHNESS_HOURS`, дефолт `72`), `cloudpayments_webhook_rate_limit_per_ip` (`CLOUDPAYMENTS_WEBHOOK_RATE_LIMIT_PER_IP`, дефолт `120`).
- `src/app/api_gateway/rate_limit.py` — `enforce_cloudpayments_webhook_limits(*, ip: str | None)` (образец `enforce_auth_limits`: per-IP, fail-open, бакет `unknown`).
- `src/app/api_gateway/routers/billing_cloudpayments.py` — на вебхуке добавить rate-limit `enforce_cloudpayments_webhook_limits(ip=client_ip(request))` → `429`; зависимость `require_cloudpayments_webhook` остаётся (наблюдательная).
- `src/app/errors.py` — `CloudPaymentsVerificationUnavailableError` (`status_code=500`, code `cloudpayments_verification_unavailable`).
- `src/app/deps.py` — фабрика `get_cloudpayments_verify_client()` (нужен `get_settings()`).
- **Миграции — НЕТ** (`cloudpayments_webhook_events`/ledger/subscriptions уже есть; колонка `transaction_id` репурпозится под broadapps `payment_id`, [Q-054-3](../../99-open-questions.md)).

### Поток (ADR-054)
```mermaid
sequenceDiagram
    participant B as broadapps (колбэк, без auth)
    participant R as Router (/webhook, public + rate-limit)
    participant S as CloudPaymentsWebhookService
    participant V as CloudPaymentsVerifyClient
    participant A as broadapps API (/users/{deviceId}/payments)
    participant DB as PostgreSQL
    B->>R: POST (без Authorization)
    R->>R: enforce_cloudpayments_webhook_limits(ip) иначе 429 ; require_cloudpayments_webhook = наблюдательный (не 401)
    R->>S: handle(raw)
    S->>S: CLOUDPAYMENTS_API_TOKEN пуст? → 500 misconfigured
    S->>S: parse: гейт(Status=Completed&OperationType=Payment) → X=AccountId/Data.user_id (UUID,lower=deviceId)
    S->>DB: резолв X→userId (ADR-053: users→auth_devices→user_not_found)
    alt user_not_found (без GET)
        S-->>R: ignored/user_not_found (200, WARNING)
    else userId резолвнут
        S->>V: list_payments(device_id=X)
        V->>A: GET /users/{X}/payments  (Bearer CLOUDPAYMENTS_API_TOKEN, 15s)
        alt таймаут / 5xx / malformed
            A-->>V: api_error
            S-->>R: 500 РЕТРАИБЕЛЬНО (лог verify=api_error, WARNING) — НЕ начисляем
        else 2xx data[] (или 404 → пустой data → no_creditable_payment)
            A-->>V: {data:[{payment_id,status,product{code,payment_type},paid_at,...}]}
            S->>S: select_creditable_payments: status∈PAID_STATUSES(succeeded) & paid_at≥now-freshness
            alt пусто
                S-->>R: ignored/no_creditable_payment (200, WARNING, лог paymentStatuses)
            else ≥1 платёж
                loop по каждому payment
                    S->>DB: BEGIN → INSERT cloudpayments_webhook_events(transaction_id=payment_id) ON CONFLICT DO NOTHING RETURNING
                    alt уже начислен
                        S->>DB: пропустить (duplicate для payment)
                    else новый
                        S->>S: класс по product.payment_type (one_time→tokens|subscription→subscription); сумма по product.code из серверных карт (anti-tamper)
                        S->>DB: WalletService.grant(idem="cp-txn:{payment_id}") [+upsert subscriptions] → audit → COMMIT
                    end
                end
                S-->>R: applied (creditedCount≥1) | duplicate (все дубли)
            end
        end
    end
    R-->>B: 200 {"code":0}  (или 429/500)
```

### Реконсиляция (детали для backend)
- **Исходящий `GET`** `{settings.cloudpayments_api_base}/users/{str(X)}/payments`, headers `Authorization: Bearer {settings.cloudpayments_api_token}`, `Accept: application/json`, таймаут 15с, per-call `httpx.AsyncClient`. `{X}` = провалидированный `uuid.UUID` (SSRF-safe; хост из config).
- **broadapps `404`** = «у пользователя нет платежей» (перманентно) → трактовать как **пустой `data` (`[]`)** → `no_creditable_payment` (`200`), **НЕ** `500`-retry. Точное поведение `404` — сверить живьём ([Q-054-2](../../99-open-questions.md)).
- **Ошибки → `api_error`** (таймаут/connect/**`5xx`**/не-JSON/нет ключа `data`) → `raise CloudPaymentsVerificationUnavailableError` (500, ретраибельно). **НЕ** проксировать наружу тело/токен.
- **`select_creditable_payments`** (чистая): для `p ∈ data` оставить, если `str(p["status"]).strip().lower() ∈ paid_statuses` (дефолт `{"succeeded"}`) **И** `parse(p["paid_at"]) >= now - timedelta(hours=freshness)` **И** валидные `p["payment_id"]` и `p["product"]["code"]`.
- **По каждому платежу (своя транзакция, идемпотентно):**
  - класс: `p["product"]["payment_type"]` — `"one_time"`→tokens; `"subscription"`→subscription; иное → пропустить (`unknown_payment_type`, WARNING).
  - сумма (anti-tamper, по `product.code`): tokens `settings.token_products().get(code)` (None/≤0 → пропустить `unknown_product`, WARNING); subscription `settings.cloudpayments_product_tokens().get(code) or settings.cloudpayments_subscription_tokens_grant`.
  - идемпотентность: dedup INSERT `cloudpayments_webhook_events(transaction_id=payment_id, user_id=<resolved>, product_id=code, kind, payload=<санитизир.>) ON CONFLICT DO NOTHING RETURNING`; пусто → пропустить; иначе `WalletService.grant(user_id=<resolved>, amount, idempotency_key=f"cp-txn:{payment_id}", reason, meta)`; subscription → upsert `subscriptions ON CONFLICT(user_id)` (`plan=code`, `expires_at`=`now()`+интервал по `_compute_expiry`, unit инферится из `code`, наследует [ADR-050 §Expiry](../../adr/ADR-050-cloudpayments-webhook.md)/[Q-050-3](../../99-open-questions.md)); audit `cloudpayments_payment`; COMMIT.
- **Окно свежести** (`CLOUDPAYMENTS_PAYMENT_FRESHNESS_HOURS`, дефолт 72ч): референс = `now()` (не `paid_at`/`DateTime` из тела). Отсекает начисление старой истории на первом колбэке (см. [ADR-054 §Окно свежести](../../adr/ADR-054-cloudpayments-webhook-payment-verification.md)).
- **Идемпотентность = broadapps `payment_id`** (ledger `cp-txn:{payment_id}` + dedup-колонка `transaction_id`:=`payment_id`), НЕ callback `TransactionId` ([ADR-054 §3](../../adr/ADR-054-cloudpayments-webhook-payment-verification.md)).

### Авторизация (ADR-054 — non-blocking, снятие 401)
`require_cloudpayments_webhook` больше **не выдаёт `401`**: broadapps шлёт колбэк без auth. Зависимость сохраняется как **наблюдательная** — читает сырой `Authorization`, вычисляет `matched`/`authScheme` **только для лога** `cloudpayments_webhook_auth_observed` (DEBUG/INFO, allowlist как [ADR-052 §3](../../adr/ADR-052-cloudpayments-webhook-lenient-auth-header.md)), всегда пропускает. Гейт активации инстанса — теперь `CLOUDPAYMENTS_API_TOKEN` (пуст → `500` misconfigured в `handle`); `CLOUDPAYMENTS_WEBHOOK_TOKEN` — легаси/опционален (не гейтит). Разделы §Авторизация (ADR-052) ниже — **исторические** (терпимый разбор + 401 отменены ADR-054).

---

## Файлы (целевая раскладка)
- `src/app/billing_cloudpayments/__init__.py`
- `src/app/api_gateway/routers/billing_cloudpayments.py` — эндпоинт `POST /v1/billing/cloudpayments/webhook` (`router`, prefix `/v1/billing/cloudpayments`, tag `Billing (CloudPayments)`), per-route bearer-dependency, сырое тело, делегирование в `CloudPaymentsWebhookService`, ответ `{"code": 0}`. (Роутеры проекта — в `api_gateway/routers/`, не в пакете модуля.)
- `src/app/billing_cloudpayments/auth.py` — `require_cloudpayments_webhook` (терпимый разбор `Authorization` из сырого заголовка + constant-time сравнение, [ADR-052](../../adr/ADR-052-cloudpayments-webhook-lenient-auth-header.md)) + `_extract_webhook_credential`/`_auth_scheme_label`/`_log_auth_denied` + `CloudPaymentsWebhookMisconfiguredError` (override `status_code = 500`, code `cloudpayments_webhook_misconfigured`). 401 при mismatch/нет токена (+ WARNING `cloudpayments_webhook_auth_denied`), 500 при незаданном секрете. См. §Авторизация ниже.
- `src/app/billing_cloudpayments/parser.py` — чистые функции дефенсивного парсинга + `ParsedPayment` (dataclass), `classify_product`, санитизация payload, константы паттернов.
- `src/app/billing_cloudpayments/service.py` — `CloudPaymentsWebhookService` + `WebhookOutcome`: дедуп, классификация, транзакция (upsert subscription | грант tokens + audit), `_log_outcome`/`_level_for`.
- `src/app/schemas/billing_cloudpayments.py` — `CloudPaymentsWebhookResponse` (`{code: int}`, только для OpenAPI; тела запроса нет).
- OpenAPI-схема `cloudpayments_webhook_scheme` (`HTTPBearer`, `scheme_name="cloudPaymentsWebhook"`, `auto_error=False`) — в `src/app/api_gateway/openapi_security.py`.
- Фабрика `get_cloudpayments_webhook_service` — в `src/app/deps.py`.
- Модель `CloudPaymentsWebhookEvent` — в `src/app/models/tables.py`.
- Миграция `0014` — `migrations/versions/…_0014_cloudpayments_webhook_events.py` (`down_revision="0013"`).
- Config — `src/app/config.py` (3 новых поля + `cloudpayments_product_tokens()`).
- Audit — `EVENT_CLOUDPAYMENTS_PAYMENT = "cloudpayments_payment"` в `src/app/audit/service.py`.
- Регистрация роутера в `src/app/main.py` (`include_router`).

## Поток обработки

```mermaid
sequenceDiagram
    participant B as broadapps (CloudPayments fmt)
    participant R as Router (/v1/billing/cloudpayments/webhook)
    participant S as CloudPaymentsWebhookService
    participant DB as PostgreSQL (1 транзакция)
    B->>R: POST + Authorization (Bearer/Token/сырой <token>)
    R->>R: lenient-parse header → constant-time compare (401 + WARNING auth_denied / 500-if-unset)
    R->>R: raw = await request.body()  (без Pydantic)
    R->>S: handle(raw)
    S->>S: parse: empty/json/object → gate(Status,OperationType) → TransactionId → Data(json.loads) → product_id → AccountId→X(UUID,lower)
    S->>DB: резолв X (ADR-053): (a) users[X]? → userId=X ; (b) иначе auth_devices[X].user_id? → userId=<это> ; (c) иначе → user_not_found
    alt любая невалидность / user_not_found / unknown_product
        S-->>R: outcome ignored/<reason>
    else валидный платёж (userId = резолвнутый)
        S->>DB: BEGIN
        S->>DB: INSERT cloudpayments_webhook_events ON CONFLICT(transaction_id) DO NOTHING RETURNING
        alt конфликт (дубликат) — RETURNING пуст
            S-->>R: outcome duplicate
        else вставлено
            S->>DB: classify_product → subscription: upsert subscriptions(active) ; tokens: (без изм. subscriptions)
            S->>DB: WalletService.grant(idem="cp-txn:{TransactionId}")
            S->>DB: audit cloudpayments_payment (assert_no_secrets, санитизировано)
            S->>DB: COMMIT
            S-->>R: outcome applied
        end
    end
    R-->>B: HTTP 200 {"code": 0}   (или 401/500)
    Note over S,DB: любой сбой → ROLLBACK → 500 → агрегатор ретраит → чистая переобработка
```

## Резолв пользователя — двухступенчатый (deviceId → userId, [ADR-053](../../adr/ADR-053-cloudpayments-webhook-user-resolution-via-auth-devices.md))

**Файл:** `src/app/billing_cloudpayments/service.py` — новый метод резолва (например `_resolve_user`), заменяет прежний одноступенчатый `_user_exists` (Stage 3 в `handle()`). **`parser.py` НЕ трогать** (резолв — DB-логика сервиса, не чистый парсинг).

**Причина ([ADR-053 §Context](../../adr/ADR-053-cloudpayments-webhook-user-resolution-via-auth-devices.md)).** broadapps на RU-флоу присылает как `AccountId`/`Data.user_id` **deviceId** (id устройства, напр. `55cbe083-...`), а НЕ наш JWT `userId` (напр. `b0f407bd-...`). Прежний lookup искал `X` **только в `users`** → deviceId там нет → `user_not_found` → оплата без начисления. Связь deviceId→userId хранится в **нашей** таблице `auth_devices(device_id PK, user_id FK→users)` ([ADR-018](../../adr/ADR-018-embedded-auth-issuer.md), [03-data-model.md §18](../../03-data-model.md)).

**Вход:** `X` — уже распарсенный, нормализованный к lower `uuid.UUID` (из `AccountId`→fallback `Data.user_id`, [§Дефенсивный парсинг](#дефенсивный-парсинг-точный-порядок-источников)). Парсинг `X` **не меняется**.

**Порядок (детерминированный, первое совпадение выигрывает):**
```
(a) SELECT 1 FROM users WHERE id = :x               -- найдено → resolved_user_id = X ;      resolved_via = "user_id"
(b) SELECT user_id FROM auth_devices WHERE device_id = :x  -- найдено → resolved_user_id = <row.user_id> ; resolved_via = "device_id"
(c) ни то, ни другое                                 -- → ignored/user_not_found (200 {"code":0} + WARNING, БЕЗ провижининга)
```
- **`:x`** передаётся как строка `str(X)` (уже lower UUID). `auth_devices.device_id` — `TEXT PK`; deviceId — UUID-строка в нижнем регистре (iOS-клиент шлёт lower и в `/v1/auth/register`, и в broadapps → сопоставление совпадает).
- Метод возвращает `(resolved_user_id: uuid.UUID, resolved_via: str)` или `None` (= `user_not_found`).
- **(a) приоритетнее (b)** — обратная совместимость: если broadapps когда-нибудь пришлёт настоящий `userId` (намерение [ADR-051](../../adr/ADR-051-cloudpayments-checkout-payment-link.md)), поведение прежнее.

**После резолва:** в `ParsedPayment.user_id` кладётся **`resolved_user_id`** (наш `userId`), НЕ исходный `X`/deviceId. Всё дальнейшее ([§Маппинг → эффект](#маппинг--эффект-_apply-после-успешного-дедуп-insert), дедуп-INSERT, upsert subscription, `WalletService.grant`, audit) — на резолвнутый `userId`. **Идемпотентность `cp-txn:{TransactionId}` и anti-tamper — БЕЗ изменений** (ключ по `TransactionId`, не по userId). `resolved_via` пробрасывается в outcome-лог ([08-observability.md](08-observability.md)).

**Безопасность/консистентность ([ADR-053 §4](../../adr/ADR-053-cloudpayments-webhook-user-resolution-via-auth-devices.md), [05-security.md](../../05-security.md#cloudpaymentsbroadapps-webhook-авторизация-ru-путь-изолированная-adr-050)):** маппинг deviceId→userId — **только** из нашей `auth_devices` (телу колбэка не доверяем, из тела — лишь сам `X`); `X` вне `users` **и** вне `auth_devices` → `user_not_found` (не создавать пользователей/устройства, [ADR-007](../../adr/ADR-007-lazy-user-provisioning.md)); оба lookup — в **той же** транзакции; `auth_devices`-строка стабильна (создаётся при `/v1/auth/register` до оплаты).

**Скоуп:** только этот метод в CloudPayments-`service.py`. **НЕ трогать:** Adapty-вебхук (там `customer_user_id` = наш `userId`, своя семантика), checkout ([ADR-051](../../adr/ADR-051-cloudpayments-checkout-payment-link.md), берёт `userId` из JWT `sub`), миграции (`auth_devices` уже есть, миграция `0005`), парсер, дедуп, идемпотентность, anti-tamper, auth-заголовок ([ADR-052](../../adr/ADR-052-cloudpayments-webhook-lenient-auth-header.md)).

## Авторизация (детали для backend) — терпимый разбор заголовка ([ADR-052](../../adr/ADR-052-cloudpayments-webhook-lenient-auth-header.md))

Файл: `src/app/billing_cloudpayments/auth.py`. Дополнительно правится описание схемы в `src/app/api_gateway/openapi_security.py`. **Роутер `billing_cloudpayments.py` не меняется** (`dependencies=[Depends(require_cloudpayments_webhook)]` остаётся). **`deps.py` не меняется** (`Request` инъектируется FastAPI автоматически).

### Сигнатура зависимости
```python
def require_cloudpayments_webhook(
    request: Request,
    _scheme: Annotated[
        HTTPAuthorizationCredentials | None, Depends(cloudpayments_webhook_scheme)
    ] = None,
) -> None:
    ...
```
- `request: Request` — **новый** параметр, чтобы читать сырой заголовок и имена заголовков для диагностики.
- `_scheme` — **декоративный** (неиспользуемый) параметр: сохраняет вклад `cloudPaymentsWebhook` в OpenAPI (замок/Authorize). Его извлечённый credential **НЕ используется** для проверки (именно `HTTPBearer` и отбивал сырой токен). Реальная проверка — из сырого заголовка.

### Терпимое извлечение (чистая функция)
```python
def _extract_webhook_credential(authorization: str | None) -> str | None:
    if authorization is None:
        return None
    value = authorization.strip()
    if not value:
        return None
    parts = value.split(None, 1)          # первая группа пробелов
    if len(parts) == 2 and parts[0].lower() in ("bearer", "token"):
        rest = parts[1].strip()
        return rest or None               # "Bearer <token>" / "Token <token>" (ci к слову)
    return value                          # «сырой» <token> ИЛИ нераспознанная схема → весь заголовок
```
- `Bearer <token>` (регистронезависимо), `Token <token>` → вторая часть.
- `Authorization: <token>` (одна часть, без пробелов) → весь заголовок.
- Нераспознанная схема (`Basic xxx`) → весь заголовок (не совпадёт → 401, fail-closed).

### Проверка (semantics сохранены)
```python
secret = get_settings().cloudpayments_webhook_token
if not secret:
    raise CloudPaymentsWebhookMisconfiguredError("cloudpayments webhook token not configured")  # 500
candidate = _extract_webhook_credential(request.headers.get("authorization")) or ""
matched = hmac.compare_digest(candidate, secret)   # ОБА пути (нет заголовка / неверный токен) проходят compare
if not matched:
    _log_auth_denied(request)                       # WARNING, безопасный allowlist (ниже)
    raise UnauthorizedError("invalid cloudpayments webhook token")  # 401, причина не раскрыта
```
- `candidate or ""` + всегда-`compare_digest` → нет ветвевого timing-leak между «нет заголовка» и «неверный токен»; оба → `401`.
- `settings.cloudpayments_webhook_token == ""` → **500** (текст `"cloudpayments webhook token not configured"`).
- **Per-route** (Depends), не глобальный middleware. Изолировано от JWT / admin / Adapty-контуров.
- Секрет/токен **никогда** не логируются.

### Диагностический лог на 401 ([ADR-052 §3](../../adr/ADR-052-cloudpayments-webhook-lenient-auth-header.md))
Ровно один WARNING-лог `"cloudpayments_webhook_auth_denied"` **только на 401** (mismatch/нет заголовка). Логгер `app.billing_cloudpayments.auth`, `log_event` (образец [ADR-046](../../adr/ADR-046-adapty-webhook-outcome-logging.md)). Allowlist полей:
```python
_AUTH_HEADER_ALLOWLIST = (
    "authorization", "x-api-key", "x-signature", "x-sign",
    "x-webhook-signature", "x-content-hmac", "content-hmac", "signature",
)

def _auth_scheme_label(authorization: str | None) -> str:
    if authorization is None:
        return "none"
    value = authorization.strip()
    if not value:
        return "empty"
    parts = value.split(None, 1)
    return parts[0].lower() if len(parts) == 2 else "raw"   # НИКОГДА не значение токена
```
- `matched=False` (bool), `authScheme=_auth_scheme_label(header)`, `presentAuthHeaders=[n for n in _AUTH_HEADER_ALLOWLIST if n in request.headers]`.
- **ЗАПРЕЩЕНО:** значение токена/секрета, полный заголовок `Authorization`, значения любых заголовков, сырое тело. Только имена + слово-схема + `matched`.
- Цель: если broadapps шлёт секрет в другом заголовке (`x-api-key`) или как подпись — увидим в `presentAuthHeaders`/`authScheme` и доработаем ([Q-052-1](../../99-open-questions.md)).

### OpenAPI
- Схема `cloudpayments_webhook_scheme` (`HTTPBearer`, `auto_error=False`, `scheme_name="cloudPaymentsWebhook"`) — **остаётся** (декоративно), образец `adapty_webhook_scheme`. Обновить **только `description`**: секрет можно ввести с префиксом `Bearer`/`Token` **или** сырым.

## Дефенсивный парсинг (точный порядок источников)

Хелпер `_first_str(*candidates)` возвращает первое непустое строковое значение (принимает `int`→`str(int)`; `None`/`""` пропускает). Плоский доступ — по top-level ключам PascalCase; `data = _parse_data(body)` — распарсенный объект `Data` (см. ниже).

- **`transaction_id`** = `_first_str(body["TransactionId"])`. Нет → `ignored/missing_transaction_id`. (Первичен top-level; в `Data` его нет.)
- **`status`** = `str(body.get("Status") or "").strip().lower()`; **`operation_type`** = `str(body.get("OperationType") or "").strip().lower()`. Гейт: `status == "completed" and operation_type == "payment"` иначе `ignored/not_a_completed_payment`.
- **`Data`-парсинг** (`_parse_data(body) -> dict | None`): `d = body.get("Data")`; если `isinstance(d, str)` → `json.loads(d)` (в try; ошибка → `None`); если `isinstance(d, dict)` → `d` (дефенсивно); иначе → `None`. `None`/не-объект → `ignored/invalid_data`.
- **`user_id`-кандидат `X`** = `_first_str(body["AccountId"], data["user_id"]).lower()` → `uuid.UUID(...)`. Нет/не-UUID → `ignored/invalid_account_id`. **Нормализация lower обязательна** (приходит в верхнем регистре). `X` — это `AccountId`, который на RU-флоу равен **deviceId** (не нашему `userId`); резолв `X`→наш `userId` — двухступенчатый ([§Резолв пользователя](#резолв-пользователя--двухступенчатый-deviceid--userid-adr-053), [ADR-053](../../adr/ADR-053-cloudpayments-webhook-user-resolution-via-auth-devices.md)).
- **`product_id`** = `_first_str(data["product_id"])`. Нет/пусто → `ignored/missing_product_id`.
- **`billing_interval_unit`** = `_first_str(data["billing_interval_unit"]).lower()` (может отсутствовать у token-пакета).
- **`billing_interval_count`** = `_parse_int(data["billing_interval_count"], default=1)` (приходит строкой `"1"`; `<1`/невалид → 1).
- **`billing_phase`** = `_first_str(data["billing_phase"])` (audit-only).
- **`subscription_id`** = `_first_str(data["subscription_id"], body["SubscriptionId"])` (audit-only).
- **`is_trial_initial`/`is_trial_conversion`/`is_initial_payment`** = строго `bool` из `data[...]` (иначе `None`; audit/лог-only).
- **`amount`** = `body.get("Amount")` / **`currency`** = `_first_str(body["Currency"])` / **`test_mode`** = строго bool `body.get("TestMode")` — только для санитизированного payload/audit.
- **PII (НЕ читать в бизнес-логику, НЕ хранить):** `CardFirstSix`/`CardLastFour`/`Issuer`/`CardType`/`DateTime`/`Description` — исключены из `ParsedPayment` by-design.

`ParsedPayment` (dataclass): `transaction_id: str`, `user_id: uuid.UUID`, `product_id: str`, `status: str`, `operation_type: str`, `billing_interval_unit: str | None`, `billing_interval_count: int`, `billing_phase: str | None`, `subscription_id: str | None`, `is_trial_initial/is_trial_conversion/is_initial_payment: bool | None`, `amount: int | None`, `currency: str | None`, `test_mode: bool | None`, `kind: str` (заполняется `classify_product`).

> `status`/`operation_type` — нормализованные (lower-case) гейт-поля (`Status`/`OperationType`), сохраняются в `ParsedPayment`, т.к. санитизированный `payload`/audit ([04-data-model.md §Санитизированный payload](04-data-model.md)) содержит `status`/`operationType`. Карт-данные исключены by-design.

## Классификация продукта (`classify_product(product_id, billing_interval_unit, token_product_ids: frozenset[str]) -> str`)

`token_product_ids` передаётся **аргументом** (не резолвится внутри) — парсер остаётся набором чистых функций без импорта `settings`; ключи `settings.token_products()` резолвятся в сервисе и передаются как `frozenset`.

Детерминированный порядок (первое совпадение выигрывает):
1. `product_id in token_product_ids` (= ключи `settings.token_products()`) → `"tokens"`. (операторская карта consumable — приоритет)
2. `billing_interval_unit` непусто и `∈ {"year","month","week","day"}` → `"subscription"`. (интервал рекуррентности — сильнейший признак)
3. `re.match(r"^\d+_tokens", product_id, re.I)` → `"tokens"`. (паттерн имени; сумма — из `TOKEN_PRODUCTS`, §Грант tokens)
4. `_looks_like_subscription(product_id)` (содержит `week`/`month`/`year`/`day` **или** оканчивается на `_nottrial`/`_trial`, case-insensitive) → `"subscription"`.
5. иначе → `"unknown"` → `ignored/unknown_product` (**WARNING**).

Константы: `_TOKENS_NAME_RE = re.compile(r"^\d+_tokens", re.I)`, `_SUB_KEYWORDS = ("week","month","year","day")`, `_SUB_SUFFIXES = ("_nottrial","_trial")`, `_INTERVAL_UNITS = frozenset({"year","month","week","day"})`.

## Маппинг → эффект (`_apply`, после успешного дедуп-INSERT)

### subscription
- **upsert** `subscriptions` одним statement (образец [ADR-048](../../adr/ADR-048-admin-subscription-grant.md)):
  ```sql
  INSERT INTO subscriptions (user_id, status, plan, expires_at, updated_at)
  VALUES (:uid, 'active', :plan, :expires_at, now())
  ON CONFLICT (user_id) DO UPDATE SET
    status='active', plan=EXCLUDED.plan, expires_at=EXCLUDED.expires_at, updated_at=now()
  ```
  `plan = product_id`; `expires_at = _compute_expiry(now, unit, count)` (§Expiry).
- **Грант кредитов:** `credits = settings.cloudpayments_product_tokens().get(product_id) or settings.cloudpayments_subscription_tokens_grant`. `WalletService.grant(user_id, amount=credits, idempotency_key=f"cp-txn:{transaction_id}", reason="cloudpayments_subscription", meta={"transactionId": ..., "productId": ..., "kind": "subscription"})`.

### tokens (§Грант tokens)
- `credits = settings.token_products().get(product_id)`. **`None`/`<=0` → `ignored/unknown_product`** (WARNING; сумму НЕ угадывать из имени — anti-tamper [ADR-015](../../adr/ADR-015-consumable-token-iap.md) BR-TP-1). При этом дедуп-INSERT уже произошёл; поэтому классификацию `unknown_product` для token-паттерна проверять **до** записи мутаций — см. порядок в `_apply` ниже.
- **`subscriptions` НЕ трогается.** `WalletService.grant(..., idempotency_key=f"cp-txn:{transaction_id}", reason="cloudpayments_tokens", meta={..., "kind": "tokens"})`.

### Порядок в `_apply` (важно)
1. `classify_product` (чистая) — если `unknown` **и** это НЕ granting-путь ⇒ вернуть `ignored/unknown_product` **до** INSERT (без записи события).
2. Для `tokens`: если `token_products().get(product_id)` пусто ⇒ `ignored/unknown_product` **до** INSERT.
3. INSERT `cloudpayments_webhook_events ... ON CONFLICT DO NOTHING RETURNING`; пусто ⇒ `duplicate`.
4. subscription → upsert + grant; tokens → grant. audit. commit. → `applied`.

> Иными словами: событие записывается в журнал **только** когда оно будет реально применено (валидный, известный, начисляемый платёж) — как в billing-adapty (там `ignored` не пишет в журнал).

## Expiry (`_compute_expiry(now, unit, count) -> datetime`)
CloudPayments-тело **не** несёт явного срока. MVP-приближение по `timedelta`:
```
DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}
expires_at = now + timedelta(days=DAYS.get(unit, 30) * count)
```
Неизвестный/None `unit` при классе subscription недостижим (класс subscription требует валидного unit ИЛИ имени — если по имени, `unit is None` → дефолт `month`/30д). Календарно-точный расчёт (relativedelta) — [Q-050-3](../../99-open-questions.md); допустимо, т.к. агрегатор пришлёт продление новым `TransactionId` к реальному сроку.

## Транзакционность
Вся обработка применяемого платежа — **одна** транзакция. INSERT `cloudpayments_webhook_events ... ON CONFLICT (transaction_id) DO NOTHING RETURNING transaction_id` — точка дедупа доставки события:
- `RETURNING` пуст → дубликат → `duplicate`, мутаций нет.
- иначе → upsert subscription (для subscription) / грант → audit → commit.
Сбой на любом шаге → ROLLBACK → 500 → агрегатор ретраит; на ретрае `transaction_id` свободен (INSERT откатился) ⇒ чистая переобработка; `grant` дополнительно идемпотентен по `cp-txn:{transaction_id}`.

## Audit
Новое `EVENT_CLOUDPAYMENTS_PAYMENT = "cloudpayments_payment"`. Пишется **только на `applied`** (внутри `_apply`, после upsert/grant). На `ignored`/`duplicate` — audit НЕ пишется.
```
AuditEvent(user_id=<uuid>, event_type=EVENT_CLOUDPAYMENTS_PAYMENT, payload={
    "transactionId": transaction_id, "productId": product_id, "kind": kind,
    "semantics": kind, "status": "active"|None, "plan": plan|None, "expiresAt": <iso|null>,
    "creditsGranted": credits, "billingPhase": billing_phase, "amount": amount, "currency": currency,
    "testMode": test_mode, "subscriptionId": subscription_id})
```
**НЕ класть** карт-данные (`CardFirstSix`/`CardLastFour`/`Issuer`/`CardType`) и bearer. `assert_no_secrets` применяется в `AuditService.record`.

## Observability
Каждый вызов `handle()` — **ровно одна** запись `"cloudpayments_webhook_outcome"` (allowlist полей, уровни) — точное ТЗ в [08-observability.md](08-observability.md).
