"""OpenAPI security scheme reflection (08-api-documentation.md, R2).

Declares two security schemes so Swagger UI shows the lock icon and the Authorize button works:
- ``bearerAuth`` (HTTP Bearer JWT) — user `/v1/*` endpoints.
- ``adminToken`` (apiKey header ``X-Admin-Token``) — `/v1/admin/*` endpoints.

Both schemes are ``SecurityBase`` instances and are consumed as dependencies *inside*
`app.deps.get_current_user` (reads the Bearer credentials) and `app.api_gateway.auth.require_admin`
(reads the ``X-Admin-Token`` value). Being SecurityBase, they contribute the security scheme to
each protected operation's OpenAPI ``security`` (lock icon / Authorize) WITHOUT adding a duplicate
``authorization`` / ``X-Admin-Token`` *parameter* to the operation. Actual auth verification still
lives in those dependencies: ``auto_error=False`` keeps the schemes from raising on a
missing/malformed header, so the real 401/constant-time checks decide the outcome unchanged.
"""

from __future__ import annotations

from fastapi.security import APIKeyHeader, HTTPBearer

# scheme_name fixes the OpenAPI components.securitySchemes key to `bearerAuth`.
# bearerFormat=JWT documents the token shape; description (RU) explains the auth model.
bearer_scheme = HTTPBearer(
    scheme_name="bearerAuth",
    bearerFormat="JWT",
    auto_error=False,
    description=(
        "JWT (RS256). В claim `sub` — userId; `userId` в теле запроса обязан совпадать "
        "с `sub`, иначе `403`. Введите токен как `Bearer <JWT>` через кнопку Authorize — "
        "он применится ко всем защищённым вызовам. Реальная проверка подписи/exp/iss/aud "
        "выполняется на сервере; это объявление — только для клиента и Swagger UI."
    ),
)

# scheme_name fixes the OpenAPI components.securitySchemes key to `adminToken`.
# apiKey-in-header documents the X-Admin-Token mechanism; the real check stays in require_admin.
admin_scheme = APIKeyHeader(
    name="X-Admin-Token",
    scheme_name="adminToken",
    auto_error=False,
    description=(
        "Изолированный admin-токен. Вставьте секрет в заголовок `X-Admin-Token` через "
        "Authorize. Пользовательский JWT admin-действия не авторизует. Реальная проверка "
        "выполняется на сервере; это объявление — только для Swagger UI."
    ),
)

# scheme_name fixes the OpenAPI components.securitySchemes key to `adaptyWebhook`.
# HTTP Bearer documents the static webhook secret; the real constant-time check stays in
# require_adapty_webhook (ADR-029). Separate from bearerAuth (user JWT) and adminToken.
adapty_webhook_scheme = HTTPBearer(
    scheme_name="adaptyWebhook",
    auto_error=False,
    description=(
        "Статический bearer-секрет вебхука Adapty (`ADAPTY_WEBHOOK_SECRET`). Вызывает Adapty, "
        "не клиент. Введите секрет как `Bearer <secret>` через Authorize. НЕ пользовательский "
        "JWT и НЕ admin-токен. Реальная constant-time проверка — на сервере."
    ),
)

# scheme_name fixes the OpenAPI components.securitySchemes key to `cloudPaymentsWebhook`.
# Decorative only (ADR-054): the webhook is PUBLIC — the aggregator sends no authorization, so this
# scheme никого не блокирует; заголовок лишь наблюдается в логах. Kept so Swagger shows the lock
# icon. Separate from bearerAuth / adminToken / adaptyWebhook.
cloudpayments_webhook_scheme = HTTPBearer(
    scheme_name="cloudPaymentsWebhook",
    auto_error=False,
    description=(
        "Публичный вебхук платёжного агрегатора (вызывает агрегатор, не клиент). Заголовок "
        "`Authorization` не требуется и не блокирует приём — он лишь наблюдается в логах. "
        "Начисление выполняется только после подтверждения платежа через платёжный сервис. НЕ "
        "пользовательский JWT и НЕ admin-токен."
    ),
)
