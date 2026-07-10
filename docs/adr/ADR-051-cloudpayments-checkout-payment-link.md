# ADR-051 — Создание ссылки на RU-оплату (исходящий вызов broadapps `/payments/link`)

- Статус: Accepted
- Дата: 2026-07-03
- Связано: [ADR-050](ADR-050-cloudpayments-webhook.md) (**вход**ящий CloudPayments-вебхук — обратная половина того же RU-контура; это ADR — **исход**ящая половина), [ADR-015](ADR-015-consumable-token-iap.md) (`TOKEN_PRODUCTS`, anti-tamper BR-TP-1), [ADR-007](ADR-007-lazy-user-provisioning.md) (провижининг `users` из JWT `sub`), [ADR-017](ADR-017-shared-server-traefik-deploy.md) (per-instance секреты), [ADR-033](ADR-033-llm-provider-abstraction.md) (образец исходящего httpx-клиента к внешнему API), [ADR-004](ADR-004-blocked-http-200.md) (карта технических ошибок). Модуль [billing-cloudpayments](../modules/billing-cloudpayments/README.md).

## Context

RU-путь оплаты ([ADR-050](ADR-050-cloudpayments-webhook.md)) состоит из двух половин:
1. **Входящая** (готова, ADR-050): `POST /v1/billing/cloudpayments/webhook` — broadapps присылает нам колбэк о состоявшейся оплате; мы находим пользователя по `AccountId`/`Data.user_id` и начисляем.
2. **Исходящая** (это ADR): наш бэкенд должен **создать платёжную ссылку** — вызвать broadapps `POST /payments/link`, получить `payment_url` (ссылка YooKassa) и вернуть её iOS-клиенту для оплаты.

**Инцидент/причина.** Сейчас платёжную ссылку формирует сам клиент (или сторонний код), и `user_id`, который уходит в broadapps, **не всегда наш backend `userId`**. broadapps честно возвращает этот же `user_id` в колбэке как `AccountId`/`Data.user_id`, но вебхук ([ADR-050 §5](ADR-050-cloudpayments-webhook.md)) не находит по нему строку в `users` → `ignored/user_not_found` → **оплата «теряется»** (деньги списаны, кредиты не начислены). Корень проблемы — **клиент-контролируемый `user_id`** на исходящем вызове.

**Решение проблемы — серверная авторитетность `user_id`.** Ввести **наш** бэкенд-эндпоинт создания ссылки, который:
- берёт `userId` **из JWT `sub`** (не из тела запроса) и подставляет его как `user_id` в исходящий вызов broadapps → тот же `userId` вернётся в колбэке как `AccountId` → вебхук гарантированно найдёт пользователя;
- держит app token (Bearer к broadapps) и `app_id` **на сервере** (не в приложении) — их нельзя подменить/выудить из клиента.

**Подтверждённые заказчиком факты (вход для решения):**
- Эндпоинт broadapps: `POST https://pay.broadapps.dev/api/v1/payments/link`.
- Заголовки: `Accept: application/json`, `Authorization: Bearer <app token>`. **Тело — `multipart/form-data`** (не JSON), поля: `app_id`, `product_id`, `user_id`, `customer_email`. **`customer_email` — единственное required-поле** по спеке; мы шлём все четыре.
- `app_id` для avelyra = `481d10b0-c7ee-4eeb-8618-d3a6cd7f7b9d` (UUID приложения из дашборда broadapps).
- app token (исходящий Bearer) — «API-ключ для внешних запросов» broadapps. **Значение** совпадает с уже поставленным `CLOUDPAYMENTS_WEBHOOK_TOKEN`, **но конфиг раздельный** (см. §5, семантически разные роли).
- `customer_email` присылает iOS в запросе к нам (email покупателя).
- Ответ broadapps `201`: `{payment_id: str, payment_url: str, status: str, expires_at: null|str}`. `payment_url` — ссылка YooKassa (напр. `https://yoomoney.ru/checkout/...`). Пример: `{"payment_id":"e3d7ffe4-...","payment_url":"https://yoomoney.ru/checkout/payments/v2/contract?orderId=...","status":"pending","expires_at":null}`.

## Decision

Ввести в модуле `billing_cloudpayments` **новый JWT-защищённый эндпоинт** `POST /v1/billing/cloudpayments/checkout`, который делает **один исходящий httpx-вызов** к broadapps `POST /payments/link` и возвращает клиенту `paymentUrl`. Это **passthrough** без своей таблицы/миграции: факт оплаты по-прежнему фиксирует входящий вебхук ([ADR-050](ADR-050-cloudpayments-webhook.md)). Adapty / StoreKit / BYOK **не трогаются**; входящий вебхук-путь ADR-050 **не трогается**.

### 1. Эндпоинт и авторизация

- **`POST /v1/billing/cloudpayments/checkout`.** Имя `checkout` = «создать оплату/checkout-ссылку»; путь под тем же префиксом `/v1/billing/cloudpayments`, что и вебхук — RU-биллинг сгруппирован. (Альтернатива `/payment-link` — см. Alternatives; выбран `checkout` как более отражающий действие клиента.)
- **JWT-защита** через `CurrentUser` (тот же контур, что прочие `/v1/*`): `get_current_user` верифицирует RS256-JWT, лениво провижинит `users` ([ADR-007](ADR-007-lazy-user-provisioning.md)) и даёт `current.user_id` = `sub`.
  - Нет/невалидный JWT → **`401`** (штатно из `get_current_user`).
- **`userId` берётся ТОЛЬКО из JWT `sub`** (`current.user_id`), **не из тела** — это ключевая мера безопасности (устраняет клиент-контролируемый `user_id`, из-за которого платежи терялись). Тело **не содержит** `userId`/`appId` (server-side).
- **Rate-limit** как у прочих write-эндпоинтов: `enforce_other_limits(user_id=current.user_id)` → превышение `429` (образец `POST /v1/subscription/sync`).
- **Побочный полезный эффект:** JWT-провижининг гарантирует, что строка `users[sub]` существует **до** оплаты. Поэтому когда broadapps пришлёт колбэк с этим `userId`, вебхук ([ADR-050 §5](ADR-050-cloudpayments-webhook.md)) точно найдёт пользователя — второй барьер против `user_not_found`.

### 2. Тело запроса (StrictModel, `extra="forbid"`)

`CloudPaymentsCheckoutRequest`:
- **`productId: str`** (required, непустой) — код продукта broadapps (напр. `week_6.99_nottrial`, `yearly_49.99_nottrial`, token-пакет из `TOKEN_PRODUCTS`).
- **`customerEmail: EmailStr`** (required) — email покупателя; broadapps требует его как единственное обязательное поле.
- **Больше ничего.** `app_id`/`user_id` — серверные; лишние поля → `422` (StrictModel).

**Валидация `productId` (симметрия с вебхуком, детерминированная):** переиспользуется `classify_product` парсера ([ADR-050 §3](ADR-050-cloudpayments-webhook.md)) как **allowlist-предикат** — мы выдаём ссылку только на продукт, который вебхук потом сможет начислить:
```
kind = classify_product(productId, billing_interval_unit=None, token_product_ids=frozenset(settings.token_products()))
if kind == "unknown":                        → 422 (unknown_product)
if kind == "tokens" and token_products().get(productId, 0) <= 0:  → 422 (unknown_product)
```
На checkout `billing_interval_unit` неизвестен (он приходит только в колбэке), поэтому классификация опирается на паттерн имени (шаги 3–5 `classify_product`): подписка = содержит `week/month/year/day` или оканчивается `_nottrial`/`_trial`; tokens = в `TOKEN_PRODUCTS` или паттерн `NNN_tokens` **и** с положительным числом кредитов в карте (anti-tamper: мы не продаём то, что не сможем начислить). **`productId` не используется для расчёта суммы гранта** — только как allowlist-гейт (сумму по-прежнему определяет сервер в вебхуке из `TOKEN_PRODUCTS`/`CLOUDPAYMENTS_PRODUCT_TOKENS`).

### 3. Исходящий вызов broadapps

`CloudPaymentsCheckoutClient.create_payment_link(*, user_id: uuid.UUID, product_id: str, customer_email: str) -> CheckoutResult`:
- `httpx.AsyncClient` (per-call, `async with`), `POST {settings.cloudpayments_api_base}/payments/link`.
- **`multipart/form-data`** — в httpx это достигается параметром `files=` с кортежами `(None, value)` (НЕ `data=`, который даёт `application/x-www-form-urlencoded`):
  ```
  files = {
      "app_id":        (None, settings.cloudpayments_app_id),
      "product_id":    (None, product_id),
      "user_id":       (None, str(user_id)),
      "customer_email":(None, customer_email),
  }
  ```
  **Content-Type руками НЕ ставить** — httpx сам выставит `multipart/form-data; boundary=...`.
- Заголовки: `Authorization: Bearer {settings.cloudpayments_api_token}`, `Accept: application/json`.
- **Таймаут** — модульная константа `_CHECKOUT_TIMEOUT_SECONDS = 15.0` (connect+read; отдельного env не заводим — 3 конфига §5 достаточно).
- **Только фиксированный upstream-хост** из `cloudpayments_api_base` (config) — не из тела клиента (нет SSRF).

**Обработка ошибок → маппинг в наш код, без утечки деталей/токена клиенту:**
| Ситуация | Наш ответ | `reason` (лог) |
|---|---|---|
| `httpx.TimeoutException` | `502 upstream_error` | `timeout` |
| `httpx.RequestError` (connect/network/TLS) | `502 upstream_error` | `connect_error` |
| статус ответа не `2xx` (broadapps success = `201`; `200`/`201` принимаем) | `502 upstream_error` | `upstream_status` |
| `2xx`, но тело не-JSON / нет `payment_url` | `502 upstream_error` | `malformed_response` |
| успех | `200` + `CheckoutResponse` | `created` |

Наружу — только generic `UpstreamError` (502, `code=upstream_error`). **НИКОГДА** не проксировать в тело клиента upstream-тело/статус/наш токен.

### 4. Ответ клиенту (StrictModel)

`CloudPaymentsCheckoutResponse` — прямой проброс полей broadapps:
- **`paymentId: str`** ← `payment_id`.
- **`paymentUrl: str`** ← `payment_url` (тип `str`, **не** `HttpUrl`: URL приходит из доверенного upstream, строгая валидация могла бы отвергнуть валидный-но-необычный URL и превратить успешную ссылку в 502; пробрасываем как есть).
- **`status: str`** ← `status` (напр. `pending`).
- **`expiresAt: str | None`** ← `expires_at` (nullable; проброс строкой без парсинга в datetime — формат upstream не фиксирован, passthrough безопаснее).

**HTTP-код успеха — `200`** (не `201`). Обоснование: эндпоинт **не создаёт долговременный ресурс в НАШЕЙ системе** (нет строки, нет `Location`, по которому клиент мог бы сделать GET) — он возвращает представление внешней ссылки. `201 Created` подразумевал бы адресуемый ресурс под нашим namespace, которого нет. Внутренний `201` от broadapps — деталь реализации, не наружу.

### 5. Config (per-instance) — 3 новых поля

Рядом с блоком CloudPayments-вебхука в `config.py`:
- **`cloudpayments_api_base: str`** (alias `CLOUDPAYMENTS_API_BASE`, default `https://pay.broadapps.dev/api/v1`) — базовый URL broadapps (public, не секрет). Исходящий POST идёт на `{base}/payments/link`.
- **`cloudpayments_app_id: str`** (alias `CLOUDPAYMENTS_APP_ID`, default `""`) — UUID приложения broadapps (avelyra = `481d10b0-c7ee-4eeb-8618-d3a6cd7f7b9d`). Не секрет, но серверный (не в клиенте).
- **`cloudpayments_api_token: str`** (alias `CLOUDPAYMENTS_API_TOKEN`, default `""`) — **секрет**: исходящий Bearer к broadapps (avelyra = app token; значение совпадает с `CLOUDPAYMENTS_WEBHOOK_TOKEN`, но конфиг раздельный).

**Почему отдельный `CLOUDPAYMENTS_API_TOKEN`, а не переиспользование `CLOUDPAYMENTS_WEBHOOK_TOKEN`:** это **семантически разные роли**, даже если значение сейчас совпадает:
- `CLOUDPAYMENTS_WEBHOOK_TOKEN` — то, чем **broadapps авторизуется к НАМ** (входящий колбэк, ADR-050);
- `CLOUDPAYMENTS_API_TOKEN` — то, чем **МЫ авторизуемся к broadapps** (исходящий вызов).

Раздельные конфиги позволяют независимую ротацию каждой стороны и не привязывают исходящую авторизацию к входящей (если broadapps позже разведёт эти ключи — правки конфига не нужно).

**Гейт «не сконфигурировано» → `503` (feature not available on this instance):** если `cloudpayments_app_id == "" ИЛИ cloudpayments_api_token == ""` → эндпоинт отдаёт **`503`** (`CloudPaymentsCheckoutNotConfiguredError(ServiceUnavailableError)`, `status_code=503`, `code=cloudpayments_checkout_not_configured`, текст «cloudpayments checkout not configured»). ⇒ эндпоинт **активен только на avelyra** (как вебхук).

**Почему `503`, а не `500` (как у вебхука ADR-050 §1):** вебхук вызывает **машина** (broadapps), которой `500` сигналит «ретрай позже». Checkout вызывает **наш iOS-клиент**; `503 service_unavailable` — честный «фича недоступна на этом инстансе» без намёка на баг сервера, консистентно с прочими не-сконфигурированными user-facing эндпоинтами ([ADR-043](ADR-043-sign-in-with-apple.md) Apple → 503, [ADR-018](ADR-018-embedded-auth-issuer.md) issuer → 503). Чёткое разведение кодов: `503` = не сконфигурировано на инстансе; `502` = broadapps недоступен/ответил ошибкой.

### 6. Наблюдаемость и PII

- Каждый вызов checkout пишет **ровно одну** структурную запись `"cloudpayments_checkout_outcome"`. **Allowlist полей:** `result` (`created`|`error`), `reason` (на ошибке), `userId` (наш UUID), `productId`, `status` (broadapps-статус на успехе), `paymentId` (на успехе).
- **ЗАПРЕЩЕНО логировать:** `customer_email` (**PII**), `CLOUDPAYMENTS_API_TOKEN`/Bearer (секрет; `authorization` уже в redaction-денилисте), `app_id` (не полезно), upstream-тело целиком.
- **Без персиста/audit-строки на MVP** (log-only): checkout — passthrough без долговременного состояния; долговременный факт (оплата) уже аудитит вебхук (`cloudpayments_payment`, ADR-050 §7). Добавлять audit-строку на создание ссылки = связать внешне-вызывающий эндпоинт с записью+commit в БД, увеличив поверхность отказа при малой пользе (структурный лог уже даёт наблюдаемость). Опциональный audit `cloudpayments_checkout` (без email) — возможное будущее для мониторинга злоупотреблений созданием ссылок.

### 7. Безопасность (сводно)

- `userId` только из JWT `sub` (не клиентский) → устраняет корень «потерянных платежей».
- `app_id` и `api_token` серверные, не в клиенте; `api_token`/email не в логах/ответе; `api_token` не проксируется наружу при ошибке upstream.
- `customer_email` — PII: не логируется, не персистится.
- Исходящий вызов только к фиксированному хосту `CLOUDPAYMENTS_API_BASE` (config) — нет SSRF.
- Полная изоляция от входящего вебхук-пути (ADR-050): разные функции/файлы, разные конфиги авторизации (`API_TOKEN` исходящий ≠ `WEBHOOK_TOKEN` входящий).

### 8. Без миграций

Passthrough-эндпоинт: своей таблицы нет, `down_revision`/миграции не заводятся. Существующая `0014` (ADR-050) и прочие — не трогаются.

### 9. Совместимость

- **Не трогаются:** входящий вебхук `POST /v1/billing/cloudpayments/webhook` (ADR-050), Adapty-webhook, StoreKit `/v1/subscription/sync`, `/v1/tokens/purchase`, BYOK, LLM-абстракция, policy-engine, схемы БД, миграции.
- **Per-instance активация:** без `CLOUDPAYMENTS_APP_ID`/`CLOUDPAYMENTS_API_TOKEN` → `503` ⇒ активен только на avelyra.
- **Операторский шаг iOS:** приложение перестаёт формировать платёжную ссылку само; вместо этого вызывает `POST /v1/billing/cloudpayments/checkout` с JWT и `customerEmail`, открывает возвращённый `paymentUrl`.

### 10. Swagger-чистота ([08-api-documentation §R2ter](../08-api-documentation.md))

User-facing OpenAPI-тексты (`summary`/`description` роута, `Field`/примеры `CloudPaymentsCheckoutRequest`/`CloudPaymentsCheckoutResponse`) — **без** ADR/TD/Q-ссылок и внутреннего жаргона (имена конфигов-секретов/сервисов/namespace). Примеры `productId`/`customerEmail` — плейсхолдеры (`week_6.99_nottrial` / `user@example.com`). Backend обязан соблюдать (см. [07-implementation-phases](../modules/billing-cloudpayments/07-implementation-phases.md)).

## Consequences

**Плюсы:**
- Устранён корень «потерянных RU-платежей»: `userId` серверный (из JWT), гарантированно совпадает с адресатом гранта в колбэке; JWT-провижининг создаёт `users`-строку до оплаты (второй барьер против `user_not_found`).
- app token и `app_id` уходят из клиента на сервер — их нельзя подменить/выудить.
- Passthrough без миграции/таблицы — минимальная поверхность; вся долговременная логика начисления остаётся в проверенном вебхуке ADR-050.
- Валидация `productId` симметрична вебхуку (общий `classify_product`) — checkout не выдаёт ссылку на не-начисляемый продукт.
- Полная изоляция от вебхук-пути; раздельные конфиги входящей/исходящей авторизации → независимая ротация.

**Минусы / риски / долг:**
- Точный контракт исходящего вызова (имена multipart-полей, `201`-shape) взят из спеки заказчика — **проверить живьём** после деплоя ([Q-051-1](../99-open-questions.md)).
- `CLOUDPAYMENTS_API_TOKEN` дублирует значение `CLOUDPAYMENTS_WEBHOOK_TOKEN` (по факту сейчас), но хранится отдельно — оператор должен задать оба на avelyra.
- Ещё один секрет-конфиг в secret-manager (per-instance).
- `EmailStr` требует пакет `email-validator` — сделать явной зависимостью (см. ТЗ Фаза 1), не полагаться на транзитивную.

## Alternatives (отвергнуто)

- **Оставить формирование ссылки на клиенте (текущее состояние).** Отвергнуто: клиент-контролируемый `user_id` → колбэк не находит пользователя → потерянные платежи; app token утёк бы в приложение. Это и есть чинимый инцидент.
- **Переиспользовать `CLOUDPAYMENTS_WEBHOOK_TOKEN` как исходящий Bearer.** Отвергнуто в пользу отдельного `CLOUDPAYMENTS_API_TOKEN`: семантически разные роли (нас→broadapps vs broadapps→нас); раздельные конфиги дают независимую ротацию и устойчивость к тому, что broadapps позже разведёт ключи. Значение сейчас совпадает — допустимо.
- **Имя пути `/payment-link` вместо `/checkout`.** Оба валидны; выбран `/checkout` как отражающий действие пользователя (инициировать оплату), короче, параллелен продуктовому термину. `/payments/link` — путь на стороне broadapps, не копируем его в наш контракт.
- **`201 Created` на успех.** Отвергнуто: мы не создаём адресуемый ресурс в нашем namespace (нет `Location`/GET). `200` + представление ссылки — честнее и проще для iOS.
- **`HttpUrl` для `paymentUrl` / парсинг `expiresAt` в `datetime`.** Отвергнуто: строгая валидация полей доверенного upstream рискует превратить успешную оплату в `502` на необычном-но-валидном значении; passthrough строкой устойчивее.
- **Audit-строка на создание ссылки.** Отложено: passthrough без долговременного состояния; факт оплаты аудитит вебхук. Log-only на MVP; audit — возможное будущее для anti-abuse.
- **Проектировать прочие ручки broadapps** (user subscription / user payments / subscription cancel / app payment stat). Вне scope этой задачи; при необходимости (напр. отмена подписки из приложения) — отдельный ADR.
