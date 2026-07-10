# API Gateway — Implementation Phases

| Phase | Задача | Зависит от |
|---|---|---|
| GW-1 | App factory, config (pydantic-settings), `/health`, `/ready`. | — |
| GW-2 | Correlation id middleware + структурированное логирование с redaction. | GW-1 |
| GW-3 | JWT auth dependency (JWKS, RS256), `get_current_user`, сверка `userId==sub`. | GW-1, Q-005-1 (дефолт) |
| GW-4 | Size-лимиты (ASGI body + поле-специфичные валидаторы). | GW-1 |
| GW-5 | Rate limit middleware (Redis). | GW-1 |
| GW-6 | Регистрация роутеров модулей, response/error mapping (ADR-004). | GW-2..GW-5 |
| GW-7 | `/metrics` + Observability middleware (метрики/трейсы). | GW-2 |
| GW-8 | `GET /healthz` — алиас `/health` (`200 {status:"ok"}`, публичный, без auth) для healthcheck Traefik/smoke ([ADR-017](../../adr/ADR-017-shared-server-traefik-deploy.md)). Минимальная правка health-router + регистрация в `main.py`. | GW-1 |

Порядок реализации backend: GW-1..GW-3 — до любых бизнес-модулей (нужны auth и роутинг).
