# billing-adapty / 06 — RBAC / Authorization

## Контур авторизации
Эндпоинт `POST /v1/billing/adapty/webhook` — **четвёртый**, machine-to-machine контур (наряду с пользовательским JWT, admin-токеном, preview signed URL). Вызывается только сервисом Adapty.

| Аспект | Значение |
|---|---|
| Механизм | статический bearer-секрет `Authorization: Bearer <ADAPTY_WEBHOOK_SECRET>` |
| Сравнение | constant-time `hmac.compare_digest` (образец `auth.py:99-134`) |
| Нет токена / mismatch | `401` (причина не раскрывается) |
| Секрет не сконфигурирован | `500` (мис-конфигурация) |
| Изоляция | секрет отдельный от JWT, `ADMIN_API_SECRET`, KMS, `PREVIEW_URL_SECRET` |
| Идентичность пользователя | НЕ из токена; берётся из тела (`customer_user_id` = UUID) |
| Per-instance | у каждого инстанса свой `ADAPTY_WEBHOOK_SECRET` ([ADR-017](../../adr/ADR-017-shared-server-traefik-deploy.md)) |

## Что эндпоинт НЕ делает
- НЕ принимает пользовательский JWT, НЕ создаёт/провижинит пользователя из токена.
- НЕ даёт admin-привилегий; секрет не пересекается с admin-контуром (нет эскалации).
- НЕ доверяет `customer_user_id` как авторизации действий — это лишь адресат гранта; несуществующий → `200 ignored/user_not_found` (без создания пользователя).

## Реализация
Per-route dependency (Depends), не глобальный middleware (глобального auth-middleware нет, `main.py:196-212`). OpenAPI security-схема — http bearer с `auto_error=False` (образец `admin_scheme`).
