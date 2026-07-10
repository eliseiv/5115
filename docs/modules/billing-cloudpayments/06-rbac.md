# billing-cloudpayments / 06 — RBAC / Authorization

## Checkout — пользовательский JWT ([ADR-051](../../adr/ADR-051-cloudpayments-checkout-payment-link.md))
Эндпоинт `POST /v1/billing/cloudpayments/checkout` — **обычный пользовательский `/v1/*` контур** (`bearerAuth`, `CurrentUser`), НЕ machine-to-machine.

| Аспект | Значение |
|---|---|
| Механизм | Пользовательский JWT (RS256), `Authorization: Bearer <JWT>`; нет/невалидный → `401` |
| Идентичность | **`userId` = JWT `sub`** (`current.user_id`), **НЕ из тела** — ключевая мера (устраняет клиент-контролируемый `user_id`). Тело не содержит `userId`/`appId` |
| Провижининг | `get_current_user` лениво provision `users[sub]` ([ADR-007](../../adr/ADR-007-lazy-user-provisioning.md)) → до оплаты гарантирует, что колбэк найдёт пользователя |
| Rate-limit | `enforce_other_limits(user_id=sub)` → `429` |
| Исходящая авторизация | к broadapps — серверный `Authorization: Bearer <CLOUDPAYMENTS_API_TOKEN>` (**отдельный** от входящего `CLOUDPAYMENTS_WEBHOOK_TOKEN`; разные роли: мы→broadapps vs broadapps→мы) |
| Секреты | `CLOUDPAYMENTS_API_TOKEN` (секрет) и `CLOUDPAYMENTS_APP_ID` — серверные, не в клиенте, не в логах/ответе. `customer_email` — PII, не логируется |
| Не сконфигурировано | `CLOUDPAYMENTS_APP_ID`/`CLOUDPAYMENTS_API_TOKEN` пусты → `503` ⇒ активен только на avelyra |
| SSRF | исходящий вызов только к фиксированному `CLOUDPAYMENTS_API_BASE` (config), не из тела клиента |

## Webhook — ПУБЛИЧНЫЙ эндпоинт + верификация ([ADR-054](../../adr/ADR-054-cloudpayments-webhook-payment-verification.md))
Эндпоинт `POST /v1/billing/cloudpayments/webhook` вызывается агрегатором broadapps. **[ADR-054](../../adr/ADR-054-cloudpayments-webhook-payment-verification.md): эндпоинт ПУБЛИЧНЫЙ (нет `401`)** — broadapps шлёт колбэк **без авторизации** (`authScheme=none`) и **без подписи**. Аутентичность события устанавливается **не токеном, а верификацией платежа через broadapps API** нашим `CLOUDPAYMENTS_API_TOKEN`.

| Аспект | Значение ([ADR-054](../../adr/ADR-054-cloudpayments-webhook-payment-verification.md)) |
|---|---|
| Механизм авторизации входа | **нет блокирующей авторизации.** `require_cloudpayments_webhook` — **наблюдательная** non-blocking зависимость (читает `Authorization`, вычисляет `matched`/`authScheme` только для лога `cloudpayments_webhook_auth_observed`, всегда пропускает). `401` **не выдаётся** |
| Trust-anchor начисления | **верификация через broadapps API** `GET /users/{deviceId}/payments` (`Bearer CLOUDPAYMENTS_API_TOKEN`): ни одно начисление без подтверждённого `succeeded`-платежа |
| Rate-limit (эндпоинт публичный) | per-source-IP `enforce_cloudpayments_webhook_limits(ip=client_ip(request))` (дефолт `CLOUDPAYMENTS_WEBHOOK_RATE_LIMIT_PER_IP=120`/мин) → `429`; fail-open при недоступности Redis |
| Гейт активации инстанса | **`CLOUDPAYMENTS_API_TOKEN`** пуст → `500` misconfigured (верификация невозможна) ⇒ начисляет **только на avelyra**. `CLOUDPAYMENTS_WEBHOOK_TOKEN` — **легаси/опционален** (не гейтит, не требуется; только `matched` в логе) |
| Изоляция | `CLOUDPAYMENTS_API_TOKEN` — отдельный секрет от JWT, `ADMIN_API_SECRET`, KMS, `PREVIEW_URL_SECRET`, `ADAPTY_WEBHOOK_SECRET`; не логируется, не в ответе, только Bearer к фикс. хосту `CLOUDPAYMENTS_API_BASE` (нет SSRF) |
| Идентичность пользователя | из тела берётся только `X`=deviceId (`AccountId`/`Data.user_id`, lower/UUID); **двухступенчатый резолв deviceId→userId** ([ADR-053](../../adr/ADR-053-cloudpayments-webhook-user-resolution-via-auth-devices.md)): (a) `X∈users`→`userId=X`; (b) иначе `X∈auth_devices.device_id`→связанный `user_id`; (c) иначе `ignored/user_not_found` (**без** исходящего GET) |
| Идемпотентность / дедуп | по broadapps **`payment_id`** (ledger `cp-txn:{payment_id}` + дедуп-колонка `cloudpayments_webhook_events.transaction_id`:=`payment_id`), НЕ callback `TransactionId` |

## Что эндпоинт НЕ делает
- НЕ принимает пользовательский JWT, НЕ создаёт/провижинит пользователя из тела/колбэка.
- НЕ даёт admin-привилегий; `CLOUDPAYMENTS_API_TOKEN` не пересекается с admin/Adapty-контурами (нет эскалации).
- НЕ начисляет по телу колбэка вслепую — **только** после подтверждённого broadapps `succeeded`-платежа в окне свежести.
- НЕ доверяет `AccountId`/`Data.user_id` как авторизации действий — это лишь вход резолва адресата гранта (маппинг deviceId→userId — только из нашей `auth_devices`, не из тела); несуществующий → `200 {"code":0}` (`ignored/user_not_found`), без создания пользователя.
- НЕ обрабатывает рефанды (агрегатор их не шлёт).

## Аутентичность payload ([ADR-054](../../adr/ADR-054-cloudpayments-webhook-payment-verification.md))
- broadapps **не подписывает** тело (нет HMAC) **и не авторизует** колбэк (`authScheme=none`). Поэтому тело колбэка **само по себе не доверенно** — оно лишь **триггер**. Аутентичность = **авторитетное подтверждение broadapps** (`status=="succeeded"` в ответе `GET /users/{deviceId}/payments` нашим ключом). Форжед-колбэк максимум триггерит бесполезный `GET`→`no_creditable_payment`. Дополнительные барьеры: резолвимый deviceId (наша `auth_devices`), окно свежести, идемпотентность по `payment_id`, per-IP rate-limit. (`CLOUDPAYMENTS_API_TOKEN` — ключ НАШИХ исходящих вызовов К broadapps, **не** для аутентификации колбэка.)

## Реализация
`require_cloudpayments_webhook` — per-route dependency (Depends), **наблюдательная** (не блокирует). OpenAPI security-схема `cloudPaymentsWebhook` (http bearer, `auto_error=False`) сохраняется **декоративно** (замок в Swagger), реальной проверки токена нет ([ADR-054 §1](../../adr/ADR-054-cloudpayments-webhook-payment-verification.md)). Rate-limit и конфиг-гейт `API_TOKEN` — в роутере/`handle()`.

> **Исторически (базовый [ADR-050](../../adr/ADR-050-cloudpayments-webhook.md)/[ADR-052](../../adr/ADR-052-cloudpayments-webhook-lenient-auth-header.md), ОТМЕНЕНО [ADR-054](../../adr/ADR-054-cloudpayments-webhook-payment-verification.md)):** вебхук был machine-to-machine-контуром со статическим bearer `CLOUDPAYMENTS_WEBHOOK_TOKEN` (constant-time, `401` на mismatch, `500` если не задан; терпимый разбор [ADR-052](../../adr/ADR-052-cloudpayments-webhook-lenient-auth-header.md)); идемпотентность по `TransactionId`. Диагностика показала `authScheme=none` → 401-путь и токен-гейт сняты. [Q-052-1](../../99-open-questions.md) закрыт.
