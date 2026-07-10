# billing-cloudpayments / 07 — Implementation Phases (ТЗ backend)

Модуль реализует: **[ADR-051](../../adr/ADR-051-cloudpayments-checkout-payment-link.md)** (checkout, фазы C1–C4, реализован), **[ADR-050](../../adr/ADR-050-cloudpayments-webhook.md)** (базовый вебхук, фазы 1–7 — **исторические, частично реализованы**) и **[ADR-054](../../adr/ADR-054-cloudpayments-webhook-payment-verification.md)** (верификация платежей + публичный вебхук + резолв [ADR-053](../../adr/ADR-053-cloudpayments-webhook-user-resolution-via-auth-devices.md) — **АКТУАЛЬНЫЕ фазы W1–W6 ниже, к реализации**). Backend выполняет фазы строго по порядку. **НЕ писать код в этом документе — это ТЗ.** Точные правила — [03-architecture.md](03-architecture.md).

> **ПРИОРИТЕТ: фазы W1–W6 ([ADR-054](../../adr/ADR-054-cloudpayments-webhook-payment-verification.md)) — текущее ТЗ вебхука.** Фазы 1–7 ([ADR-050](../../adr/ADR-050-cloudpayments-webhook.md)) ниже — **исторические**: их авторизация (bearer/401), классификация паттерном имени и идемпотентность по `TransactionId` **отменены** [ADR-054](../../adr/ADR-054-cloudpayments-webhook-payment-verification.md). При расхождении — приоритет у W1–W6.

---

# Webhook (ADR-054 — АКТУАЛЬНО) — колбэк-триггер + верификация платежей

Цель: колбэк broadapps = **триггер**; начисление — только после **реконсиляции платежей** через broadapps API (`GET /users/{deviceId}/payments`) нашим `CLOUDPAYMENTS_API_TOKEN`. Эндпоинт публичный (нет `401`). **Без миграции.** Точные правила — [03-architecture.md §Верификация платежей](03-architecture.md), контракт — [02-api-contracts.md](02-api-contracts.md), RBAC — [06-rbac.md](06-rbac.md).

## Фаза W1 — Config + ошибка + rate-limit
- `src/app/config.py` (рядом с CloudPayments-блоком):
  - `cloudpayments_paid_statuses_raw: str = Field(default="succeeded", alias="CLOUDPAYMENTS_PAID_STATUSES")` + метод `cloudpayments_paid_statuses() -> frozenset[str]` (CSV **или** JSON-массив; lower-case; пусто/малформед → `{"succeeded"}`).
  - `cloudpayments_payment_freshness_hours: int = Field(default=72, alias="CLOUDPAYMENTS_PAYMENT_FRESHNESS_HOURS")` (>0; ≤0 → дефолт 72).
  - `cloudpayments_webhook_rate_limit_per_ip: int = Field(default=120, alias="CLOUDPAYMENTS_WEBHOOK_RATE_LIMIT_PER_IP")`.
  - Переиспользуются существующие `cloudpayments_api_base`/`cloudpayments_api_token` (ADR-051), `token_products()`/`cloudpayments_product_tokens()`/`cloudpayments_subscription_tokens_grant`.
- `src/app/errors.py`: `CloudPaymentsVerificationUnavailableError` (`status_code=500`, `code="cloudpayments_verification_unavailable"`). `CloudPaymentsWebhookMisconfiguredError` (500) переиспользуется, но триггерится **пустым `CLOUDPAYMENTS_API_TOKEN`** (текст «cloudpayments api token not configured»).
- `src/app/api_gateway/rate_limit.py`: `async def enforce_cloudpayments_webhook_limits(*, ip: str | None) -> bool` — образец `enforce_auth_limits` (per-IP бакет `rl:cpwebhook:{ip or "unknown"}`, лимит `cloudpayments_webhook_rate_limit_per_ip`, окно `rate_limit_window_seconds`, fail-open при `redis.RedisError`).

## Фаза W2 — Авторизация → наблюдательная (снять 401)
- `src/app/billing_cloudpayments/auth.py`: `require_cloudpayments_webhook` **больше не выдаёт `401`** — читает сырой `Authorization`, вычисляет `matched` (constant-time с легаси `CLOUDPAYMENTS_WEBHOOK_TOKEN`, если задан — **только для лога**) и `authScheme`, **всегда возвращает `None` (пропускает)**. Ровно один лог `"cloudpayments_webhook_auth_observed"` (**DEBUG/INFO**, не WARNING; allowlist `matched`/`authScheme`/`presentAuthHeaders`, [08-observability §Auth-observed](08-observability.md)). Прежний `cloudpayments_webhook_auth_denied` **удалить** (401 нет). `CloudPaymentsWebhookMisconfiguredError` из auth-dep **убрать** (гейт `API_TOKEN` теперь в `handle()`, W4). OpenAPI-схема `cloudpayments_webhook_scheme` (`auto_error=False`) остаётся декоративной.

## Фаза W3 — Verify-клиент (`src/app/billing_cloudpayments/verify.py`, **новый**)
- `_VERIFY_TIMEOUT_SECONDS = 15.0`; dataclass `CreditablePayment(payment_id: str, product_code: str, payment_type: str, status: str, paid_at: datetime)`.
- `CloudPaymentsVerifyClient(settings)`:
  - `async list_payments(*, device_id: uuid.UUID) -> list[dict]`: `httpx.AsyncClient` per-call, `GET {api_base}/users/{str(device_id)}/payments`, `headers={"Authorization":f"Bearer {api_token}","Accept":"application/json"}`, `timeout=_VERIFY_TIMEOUT_SECONDS`. **Ошибки → `CloudPaymentsVerificationUnavailableError` (500 retriable):** `httpx.TimeoutException`/`RequestError`; **не-2xx** (см. §404 ниже); не-JSON / нет ключа `data`. Возвращает `data` (list). **НЕ** проксировать тело/токен наружу.
    - **Обработка 404 ([ADR-054 §Decision п.5](../../adr/ADR-054-cloudpayments-webhook-payment-verification.md)):** `404` от broadapps = «у пользователя нет платежей» → трактовать как **пустой список** (не `api_error`): вернуть `[]` → далее `no_creditable_payment` (200), **НЕ** 500-retry. Только `5xx`/timeout/connect/невалидный JSON → `api_error` (500 retriable). (Точное поведение 404 — сверить живьём, [Q-054-2](../../99-open-questions.md).)
  - `select_creditable_payments(data, *, paid_statuses, now, freshness_hours) -> list[CreditablePayment]` (**чистая**): оставить `p`, если `str(p["status"]).strip().lower() ∈ paid_statuses` **И** `parse(p["paid_at"]) >= now - timedelta(hours=freshness_hours)` **И** валидные `p["payment_id"]`, `p["product"]["code"]`, `p["product"]["payment_type"]`.

## Фаза W4 — Сервис (`src/app/billing_cloudpayments/service.py`) — реконсиляция
- Порядок в `handle(raw)`:
  1. **конфиг-гейт:** `not settings.cloudpayments_api_token` → `raise CloudPaymentsWebhookMisconfiguredError` (500).
  2. парсинг (упрощён, W5): гейт `Status/OperationType` не прошёл → `ignored/not_a_completed_payment`; нет/не-UUID `X` → `ignored/invalid_account_id`. (`TransactionId`/`product_id` — опц. контекст, НЕ гейтят.)
  3. резолв `X`→`userId` (двухступенчатый, [ADR-053](../../adr/ADR-053-cloudpayments-webhook-user-resolution-via-auth-devices.md), метод `_resolve_user`): `None` → `ignored/user_not_found` (**WARNING**, **без** GET).
  4. `data = await verify_client.list_payments(device_id=X)` (api_error → пробросить `CloudPaymentsVerificationUnavailableError` → 500).
  5. `payments = select_creditable_payments(data, paid_statuses=..., now=utcnow, freshness_hours=...)`; пусто → `ignored/no_creditable_payment` (**WARNING**; лог `paymentStatuses`).
  6. `credited = 0`; **по каждому** `p in payments` — `await self._apply_payment(p, user_id, resolved_via)`; если начислено → `credited += 1`.
  7. итог: `credited >= 1` → `applied` (`creditedCount=credited`); иначе (все дубли) → `duplicate`.
- `_apply_payment(p, user_id, resolved_via)` (**отдельная транзакция на платёж**):
  - класс: `p.payment_type=="one_time"`→tokens; `=="subscription"`→subscription; иначе → лог `unknown_payment_type` (WARNING), пропустить (не начислено).
  - сумма (anti-tamper, по `p.product_code`): tokens `settings.token_products().get(code)` → `None`/`<=0` → `unknown_product` (WARNING), пропуск; subscription `settings.cloudpayments_product_tokens().get(code) or settings.cloudpayments_subscription_tokens_grant`.
  - `BEGIN` → INSERT `cloudpayments_webhook_events(transaction_id=p.payment_id, user_id, product_id=code, kind, payload=sanitize) ON CONFLICT DO NOTHING RETURNING` → пусто ⇒ пропуск (дубликат платежа); иначе:
    - subscription → `_upsert_subscription(user_id, active, plan=code, expires_at=_compute_expiry(now, unit_from_code, 1))` (`unit` инферится из `code`, [03-architecture.md](03-architecture.md)) + грант; tokens → грант.
    - `WalletService.grant(user_id, amount=credits, idempotency_key=f"cp-txn:{p.payment_id}", reason, meta={"paymentId":p.payment_id,"productCode":code,"kind":kind})`; audit `cloudpayments_payment`; `COMMIT`.
  - вернуть «начислено?» (bool).
- **`classify_product` в начислении НЕ вызывать** (класс — из `payment_type`); функция остаётся для checkout.

## Фаза W5 — Парсер (упростить `src/app/billing_cloudpayments/parser.py`)
- Обязательны: гейт `parse_gate(Status, OperationType)` + `parse_user_id` (`X`=AccountId→Data.user_id, **`.lower()`+UUID**).
- `TransactionId`/`product_id`/`billing_*`/trial-флаги → **опц. контекст лога** (не гейтят, не отсекают колбэк). `missing_transaction_id`/`missing_product_id`/`invalid_data` как отсекающие исходы **убрать** из пути начисления.
- Карт-данные (`CardFirstSix`/…) — по-прежнему **не** читать. `sanitize_payload` — allowlist для персиста/audit ([04-data-model.md](04-data-model.md)).

## Фаза W6 — Router + наблюдаемость
- `src/app/api_gateway/routers/billing_cloudpayments.py` (вебхук-роут):
  1. `if not await enforce_cloudpayments_webhook_limits(ip=client_ip(request)): raise RateLimitedError(...)` → `429`.
  2. `raw = await request.body()`; `outcome = await service.handle(raw)`.
  3. **всегда** `JSONResponse({"code": 0}, status_code=200)` для любого `WebhookOutcome`; `429` из rate-limit, `500` из `CloudPaymentsWebhookMisconfiguredError`/`CloudPaymentsVerificationUnavailableError`/ошибок БД. `require_cloudpayments_webhook` остаётся в `dependencies` (наблюдательная).
- `src/app/deps.py`: `get_cloudpayments_verify_client() -> CloudPaymentsVerifyClient` (только `get_settings()`); фабрика сервиса дополняется verify-клиентом.
- Наблюдаемость (`service.py`): один `"cloudpayments_webhook_outcome"` на колбэк + поля `verify`/`creditedCount`/`paymentStatuses`/`resolvedVia` ([08-observability.md](08-observability.md)). Опц. DEBUG per-payment.

## Что НЕ трогать (ADR-054)
- **Миграции — НЕТ** (`cloudpayments_webhook_events`/ledger/subscriptions уже есть; колонка `transaction_id` хранит broadapps `payment_id` — репурпозинг без DDL, [Q-054-3](../../99-open-questions.md)).
- Checkout ([ADR-051](../../adr/ADR-051-cloudpayments-checkout-payment-link.md)) — только переиспользование config/httpx-паттерна (контракт не менять). Резолв deviceId→userId ([ADR-053](../../adr/ADR-053-cloudpayments-webhook-user-resolution-via-auth-devices.md)) — не менять. Adapty/StoreKit/BYOK/policy — не трогать. anti-tamper — сумма только из серверных карт.

## Deployment (devops, после backend, ADR-054)
- Env на **avelyra**: `CLOUDPAYMENTS_API_TOKEN=<app token broadapps>` (уже есть от checkout — теперь гейтит и вебхук); опц. `CLOUDPAYMENTS_PAID_STATUSES` (дефолт `succeeded`), `CLOUDPAYMENTS_PAYMENT_FRESHNESS_HOURS` (72), `CLOUDPAYMENTS_WEBHOOK_RATE_LIMIT_PER_IP` (120). `CLOUDPAYMENTS_WEBHOOK_TOKEN` — можно **не задавать** (легаси). Callback URL в broadapps = `…/v1/billing/cloudpayments/webhook`.
- **Разовый шаг восстановления застрявших платежей ([ADR-054](../../adr/ADR-054-cloudpayments-webhook-payment-verification.md)):** платежи, «застрявшие» за инцидентный период (когда вебхук отбивал `401`), лежат вне 72-часового окна свежести. Для их восстановления — **временно** поднять `CLOUDPAYMENTS_PAYMENT_FRESHNESS_HOURS` до величины, покрывающей инцидентный период (напр. `2160`=90д), прогнать тестовые колбэки/дождаться ретраев broadapps по каждому оплатившему deviceId, затем **вернуть `72`**. Идемпотентность по `payment_id` гарантирует отсутствие двойного начисления при повышенном окне.
- **После деплоя (сверка живьём, [Q-054-1](../../99-open-questions.md)/[Q-054-2](../../99-open-questions.md)):** тестовый платёж → лог `verify=ok`, `applied` `creditedCount≥1`; сверить `paymentStatuses` (ожидается `succeeded`), поведение broadapps 404, при необходимости откалибровать `CLOUDPAYMENTS_PAID_STATUSES`/`FRESHNESS_HOURS`.

## Тестовые ориентиры (кратко, полное — [09-testing.md](09-testing.md))
- Публичный: колбэк без `Authorization` → **не `401`**, доходит до резолва/верификации.
- `CLOUDPAYMENTS_API_TOKEN==""` → `500` misconfigured; per-IP флуд → `429`.
- verify api_error (timeout/5xx/malformed) → `500` retriable, начисления нет; broadapps `404` → `no_creditable_payment` (200).
- `succeeded`-платёж в окне → `applied` `creditedCount≥1`; вне окна → не начислен; повтор колбэка → `duplicate` (идемпотентность по `payment_id`).
- класс по `payment_type`; сумма по `product.code`; неизвестный `code`/`payment_type` → платёж пропущен (WARNING).

---

# Checkout (ADR-051) — исходящий вызов broadapps `/payments/link`

---

# Checkout (ADR-051) — исходящий вызов broadapps `/payments/link`

Цель: наш JWT-эндпоинт `POST /v1/billing/cloudpayments/checkout` создаёт платёжную ссылку через broadapps; `userId` берётся из JWT (не из тела) → устраняет «потерянные платежи». **Passthrough, без миграции.** Точные детали — [03-architecture.md §Checkout](03-architecture.md), контракт — [02-api-contracts.md](02-api-contracts.md).

## Фаза C1 — Config + зависимость + схемы + ошибка
- `src/app/config.py` (рядом с блоком CloudPayments-вебхука, `config.py:155-172`):
  - `cloudpayments_api_base: str = Field(default="https://pay.broadapps.dev/api/v1", alias="CLOUDPAYMENTS_API_BASE")`
  - `cloudpayments_app_id: str = Field(default="", alias="CLOUDPAYMENTS_APP_ID")`
  - `cloudpayments_api_token: str = Field(default="", alias="CLOUDPAYMENTS_API_TOKEN")`
  - (опц.) helper `cloudpayments_checkout_configured() -> bool` = `bool(self.cloudpayments_app_id and self.cloudpayments_api_token)`.
- `pyproject.toml`: добавить `email-validator>=2,<3` (или `pydantic[email]`) в dependencies — `EmailStr` требует его; сейчас лишь транзитивно.
- `src/app/schemas/billing_cloudpayments.py`: **добавить** `CloudPaymentsCheckoutRequest` (`productId: str` non-empty, `customerEmail: EmailStr`, StrictModel) + `CloudPaymentsCheckoutResponse` (`paymentId: str`, `paymentUrl: str`, `status: str`, `expiresAt: str | None`). **Swagger-чистота:** `Field`-описания/примеры без ADR/TD/Q; примеры-плейсхолдеры (`week_6.99_nottrial` / `user@example.com`).
- `src/app/errors.py`: `CloudPaymentsCheckoutNotConfiguredError(ServiceUnavailableError)` (`status_code=503`, `code="cloudpayments_checkout_not_configured"`).

## Фаза C2 — Checkout-клиент (`src/app/billing_cloudpayments/checkout.py`)
- `CheckoutResult` dataclass (`payment_id`, `payment_url`, `status`, `expires_at: str | None`), `_CHECKOUT_TIMEOUT_SECONDS = 15.0`.
- `CloudPaymentsCheckoutClient(settings)`:
  - `validate_product(product_id) -> None`: `classify_product(product_id, None, frozenset(settings.token_products()))`; `unknown` → `ValidationFailedError` (422); `tokens` с `token_products().get(product_id,0)<=0` → `ValidationFailedError` (422). (Может жить в роутере — единая точка; сумму НЕ считает.)
  - `async create_payment_link(*, user_id, product_id, customer_email) -> CheckoutResult`: `httpx.AsyncClient` per-call, `POST {api_base}/payments/link`, **multipart через `files={"app_id":(None,...),"product_id":(None,...),"user_id":(None,str(user_id)),"customer_email":(None,...)}`** (НЕ `data=`), `headers={"Authorization":f"Bearer {api_token}","Accept":"application/json"}`, `timeout=_CHECKOUT_TIMEOUT_SECONDS`. **Content-Type руками не ставить.**
  - Ошибки → `UpstreamError` (502): `TimeoutException`→`timeout`; `RequestError`→`connect_error`; статус не 2xx→`upstream_status`; 2xx без `payment_url`/не-JSON→`malformed_response`. Наружу — generic 502, **без** upstream-тела/статуса/токена.
- **Наблюдаемость:** ровно один лог `"cloudpayments_checkout_outcome"` на вызов (allowlist: `result`/`reason`/`userId`/`productId`/`status`/`paymentId`). **НЕ логировать** `customer_email`/токен/`app_id`/upstream-тело.

## Фаза C3 — Router + deps
- `src/app/api_gateway/routers/billing_cloudpayments.py`: **добавить** `@router.post("/checkout", response_model=CloudPaymentsCheckoutResponse, ...)`:
  1. `current: CurrentUser` → `user_id = current.user_id` (**из JWT, не из тела**).
  2. config-gate: `if not settings.cloudpayments_checkout_configured(): raise CloudPaymentsCheckoutNotConfiguredError(...)` → 503.
  3. `if not await enforce_other_limits(user_id=current.user_id): raise RateLimitedError(...)` → 429.
  4. `client.validate_product(body.productId)` → 422 при unknown/некредитуемом.
  5. `result = await client.create_payment_link(user_id=current.user_id, product_id=body.productId, customer_email=body.customerEmail)`.
  6. вернуть `CloudPaymentsCheckoutResponse(paymentId=..., paymentUrl=..., status=..., expiresAt=...)` — HTTP **200**.
  - **Swagger-чистота:** `summary` (напр. «Создать ссылку на оплату (RU)»), лаконичный `description` (что делает, возвращает `paymentUrl`) — без ADR/TD/Q и имён секретов/сервисов.
- `src/app/deps.py`: `get_cloudpayments_checkout_client() -> CloudPaymentsCheckoutClient` (только `get_settings()`; без DbSession).
- `src/app/main.py` — **не трогать** (router уже включён).

## Фаза C4 — Deployment (devops, после backend)
- Env на **avelyra**: `CLOUDPAYMENTS_APP_ID=481d10b0-c7ee-4eeb-8618-d3a6cd7f7b9d`, `CLOUDPAYMENTS_API_TOKEN=<app token broadapps>` (секрет; значение = `CLOUDPAYMENTS_WEBHOOK_TOKEN`, но конфиг отдельный), `CLOUDPAYMENTS_API_BASE` — дефолт (не задавать, если стандартный). На прочих инстансах не задавать → `503` (неактивен). Рабочий образ пересобрать (появилась зависимость `email-validator`).
- **После деплоя (сверка живьём, [Q-051-1](../../99-open-questions.md)):** прислать тестовый `POST /checkout`; убедиться, что broadapps вернул `payment_url` (201-shape), а последующий колбэк нашёл пользователя.

## Что НЕ трогать (checkout)
- Входящий вебхук `POST /v1/billing/cloudpayments/webhook` (ADR-050) и его файлы (`auth.py`/`parser.py`/`service.py`).
- Adapty-webhook, StoreKit, `/v1/tokens/purchase`, BYOK, policy-engine, схемы БД, миграции.
- `customer_email`/токен — не логировать, не персистить.

---

# Webhook (ADR-050) — входящий колбэк broadapps — **ИСТОРИЧЕСКОЕ (отменено [ADR-054](../../adr/ADR-054-cloudpayments-webhook-payment-verification.md))**

> **⚠️ Эти фазы 1–7 ОТМЕНЕНЫ [ADR-054](../../adr/ADR-054-cloudpayments-webhook-payment-verification.md).** Их авторизация (Фаза 2: bearer/401/500-if-unset), классификация паттерном имени и идемпотентность по `TransactionId` (Фаза 4), а также reason'ы `missing_transaction_id`/`missing_product_id`/`invalid_data` **больше не применяются**. Актуальное ТЗ вебхука — **фазы W1–W6 выше**. Раздел сохранён для истории/трассируемости; **backend реализует W1–W6, не 1–7**. Миграция `0014` (Фаза 1) и ORM `CloudPaymentsWebhookEvent` — **уже применены** и переиспользуются W-фазами (колонка `transaction_id` хранит `payment_id`).

## Фаза 1 — Config + миграция + ORM + audit
- `src/app/config.py` (рядом с Adapty-блоком, `config.py:138-153`):
  - `cloudpayments_webhook_token: str = Field(default="", alias="CLOUDPAYMENTS_WEBHOOK_TOKEN")`
  - `cloudpayments_product_tokens_raw: str = Field(default="{}", alias="CLOUDPAYMENTS_PRODUCT_TOKENS")`
  - `cloudpayments_subscription_tokens_grant: int = Field(default=1000, alias="CLOUDPAYMENTS_SUBSCRIPTION_TOKENS_GRANT")`
  - метод `cloudpayments_product_tokens() -> dict[str, int]` — **точная копия формы** `token_products()`/`adapty_product_tokens()` (`config.py:293`): JSON `{str: positive-int}`, малформед/не-объект → `{}`, `bool` исключить, невалидные пары пропустить (graceful, не крашить процесс).
- Миграция **`0014`** (`migrations/versions/…_0014_cloudpayments_webhook_events.py`, `down_revision="0013"`): таблица `cloudpayments_webhook_events` + index по `user_id` (DDL — [04-data-model.md](04-data-model.md)). Проверить **single head** (`alembic heads`).
- ORM-модель `CloudPaymentsWebhookEvent` в `src/app/models/tables.py`.
- Audit: `EVENT_CLOUDPAYMENTS_PAYMENT = "cloudpayments_payment"` в `src/app/audit/service.py`.

## Фаза 2 — Авторизация
- `src/app/billing_cloudpayments/auth.py`: `require_cloudpayments_webhook` (constant-time bearer, образец `require_adapty_webhook`): извлечь токен после `Bearer `, `hmac.compare_digest` с `settings.cloudpayments_webhook_token`; пустой секрет → `500` (`CloudPaymentsWebhookMisconfiguredError`, `status_code=500`, code `cloudpayments_webhook_misconfigured`); mismatch/нет → `401` (`UnauthorizedError`).
- OpenAPI security-схема `cloudpayments_webhook_scheme` (http bearer, `scheme_name="cloudPaymentsWebhook"`, `auto_error=False`) в `src/app/api_gateway/openapi_security.py`, образец `adapty_webhook_scheme`.

## Фаза 3 — Парсер (`src/app/billing_cloudpayments/parser.py`, чистые функции)
Точные источники/порядок — [03-architecture.md §Дефенсивный парсинг](03-architecture.md). Реализовать:
- `_first_str(*candidates) -> str | None` (принимает `int`→`str`, пропускает `None`/`""`).
- `_parse_int(value, default) -> int` (строка `"1"`→1; `<1`/невалид → default).
- `_parse_data(body) -> dict | None` (`Data`: str→`json.loads` в try; dict→as-is; иначе None).
- `parse_transaction_id`, `parse_gate(status, operation_type) -> bool`, `parse_user_id` (**`.lower()` + UUID**), `parse_product_id`, `parse_billing_interval_unit/count`, `parse_billing_phase`, `parse_subscription_id`, `parse_trial_flags` (строго bool|None), `parse_amount/currency/test_mode`.
- `classify_product(product_id, billing_interval_unit, token_product_ids: frozenset[str]) -> "subscription"|"tokens"|"unknown"` (5 шагов, [03-architecture.md §Классификация](03-architecture.md)). `token_product_ids` передаётся аргументом (чистая функция — без импорта `settings`; сервис резолвит `settings.token_products()` и передаёт ключи как `frozenset`). Константы: `_TOKENS_NAME_RE`, `_SUB_KEYWORDS`, `_SUB_SUFFIXES`, `_INTERVAL_UNITS`.
- `sanitize_payload(parsed) -> dict` — allowlist-проекция для персиста/audit ([04-data-model.md §Санитизированный payload](04-data-model.md)); **карт-данные исключены by-design**.
- `ParsedPayment` dataclass (поля — [03-architecture.md](03-architecture.md)).
- `_compute_expiry(now, unit, count) -> datetime` (timedelta-days `{day:1,week:7,month:30,year:365}×count`; None-unit → 30д).

**НЕ** читать `CardFirstSix`/`CardLastFour`/`Issuer`/`CardType`/`DateTime`/`Description` в `ParsedPayment`.

## Фаза 4 — Сервис (`src/app/billing_cloudpayments/service.py`)
- `WebhookOutcome` (dataclass: `result: str`, `reason: str | None`). `_ignored(reason)` helper.
- `CloudPaymentsWebhookService.handle(raw: bytes) -> WebhookOutcome`:
  1. `not raw` → `ignored/empty_body`.
  2. `json.loads(raw)` (в try) → неуспех → `ignored/invalid_json`; результат не dict → `ignored/not_an_object`.
  3. `transaction_id` нет → `ignored/missing_transaction_id`.
  4. гейт `Status/OperationType` не прошёл → `ignored/not_a_completed_payment`.
  5. `_parse_data` None → `ignored/invalid_data`.
  6. `product_id` нет → `ignored/missing_product_id`.
  7. `user_id` (AccountId→Data.user_id, lower→UUID) невалиден → `ignored/invalid_account_id`.
  8. `not await self._user_exists(user_id)` → `ignored/user_not_found` (**WARNING**).
  9. `kind = classify_product(...)`: `unknown` → `ignored/unknown_product` (**WARNING**).
  10. если `kind == "tokens"` и `token_products().get(product_id)` пусто/<=0 → `ignored/unknown_product` (**WARNING**) — **до** INSERT.
  11. иначе → `await self._apply(parsed)`.
- `_apply(parsed)` (одна транзакция): INSERT-дедуп `cloudpayments_webhook_events ON CONFLICT(transaction_id) DO NOTHING RETURNING` → пусто ⇒ `duplicate`; иначе:
  - `subscription`: `_upsert_subscription(active, plan=product_id, expires_at=_compute_expiry(...))` + `_grant(reason="cloudpayments_subscription")` + audit.
  - `tokens`: `_grant(reason="cloudpayments_tokens")` (без upsert subscription) + audit.
  - commit → `applied`.
- `_grant(parsed, credits, reason)`: `WalletService.grant(user_id, amount=credits, idempotency_key=f"cp-txn:{transaction_id}", reason=reason, meta={"transactionId":..., "productId":..., "kind":...})`.
  - subscription credits = `cloudpayments_product_tokens().get(product_id) or cloudpayments_subscription_tokens_grant`.
  - tokens credits = `token_products().get(product_id)` (гарантированно >0 после шага 10).
- `_upsert_subscription`: параметризованный `INSERT ... ON CONFLICT (user_id) DO UPDATE` (образец `admin/service.py::grant_subscription`).
- Логирование исхода — Фаза 6 ([08-observability.md](08-observability.md)).

## Фаза 5 — Router + регистрация
- `src/app/api_gateway/routers/billing_cloudpayments.py`: `POST /v1/billing/cloudpayments/webhook`; сырое тело (`await request.body()`), без Pydantic body-модели; per-route `Depends(require_cloudpayments_webhook)`; вызвать `service.handle(raw)`; **всегда вернуть `JSONResponse({"code": 0}, status_code=200)`** для любого `WebhookOutcome` (result применён только в лог/audit). `401`/`500` поднимаются из auth-dependency / всплывают как ошибки БД.
- Фабрика `get_cloudpayments_webhook_service` в `src/app/deps.py`.
- Регистрация роутера в `src/app/main.py` (`include_router`, рядом с billing-adapty).
- `SizeLimitMiddleware` — стандартный лимит тела достаточен (payload платежа невелик); повышенный лимит роута НЕ требуется.
- **Swagger-чистота** ([R2ter](../../08-api-documentation.md)): `summary` (≤~60, напр. «Приём платежа RU (webhook)»), лаконичный `description` (что делает, что возвращает `{"code":0}`), схема `CloudPaymentsWebhookResponse` `Field`-ы — **без** ADR/TD/Q и внутренних имён таблиц/namespace.

## Фаза 6 — Наблюдаемость
- Только `src/app/billing_cloudpayments/service.py` (роутер не трогать). Логгер `logging.getLogger(__name__)` + `log_event(...)` (образец `app.billing_adapty.service`).
- Каждая return-точка `handle()`/`_apply()` — через `_log_outcome(outcome, *, transaction_id, product_id, user_id, kind)`; ровно один лог `"cloudpayments_webhook_outcome"` на вызов. Уровни/allowlist — [08-observability.md](08-observability.md).

## Что НЕ трогать
- Adapty-webhook (`billing_adapty/*`), StoreKit `/v1/subscription/sync`, `/v1/tokens/purchase`, BYOK, LLM-абстракцию, policy-engine.
- Схему `subscriptions`/`ledger_transactions`/`wallets` (только чтение/upsert/grant существующими сервисами).
- Существующие миграции (только новая `0014`).
- Карт-данные — не читать, не логировать, не персистить.

## Фаза 7 — Deployment (devops, после backend)
- Env `CLOUDPAYMENTS_WEBHOOK_TOKEN` (per-instance, secret manager) — задать **только на avelyra** (= app API key broadapps). `CLOUDPAYMENTS_PRODUCT_TOKENS` (JSON product→tokens тиров подписки) + `CLOUDPAYMENTS_SUBSCRIPTION_TOKENS_GRANT` (fallback). См. [07-deployment.md](../../07-deployment.md).
- **Операторский шаг:** в панели broadapps сменить Callback URL с `…/v1/billing/adapty/webhook` на `…/v1/billing/cloudpayments/webhook`.
- Миграцию `0014` применить (`docker compose run --rm migrate`) на всех инстансах (таблица создаётся везде; эндпоинт активен только там, где задан токен).
- **После деплоя (сверка живьём, [Q-050-1/2](../../99-open-questions.md)):** прислать тестовый платёж; убедиться, что broadapps принял `{"code":0}` и не ретраит; проверить логи `"cloudpayments_webhook_outcome"`.

## Тестовые ориентиры (для qa) — кратко, полное — [09-testing.md](09-testing.md)
- 401 нет/неверный bearer; 500 незаданный секрет.
- `{"code":0}` на каждый `ignored/*` (включая пустое тело, не-JSON, не-Completed, unknown_product).
- Реальный payload годовой подписки → `subscriptions active`, `plan=yearly_49.99_nottrial`, `expires_at≈+365д`, **один** ledger-грант `cp-txn:{TransactionId}`.
- token-пакет (`product_id ∈ TOKEN_PRODUCTS`) → разовый грант N, подписка не тронута.
- Дубликат `TransactionId` → `duplicate`, баланс не изменился.
- `AccountId` верхним регистром → нормализован к lower → найден пользователь.
- Карт-данные отсутствуют в `payload`/audit/логах.
