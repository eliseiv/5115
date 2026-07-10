# Module: API Gateway

- Статус: Реализован
- Ответственность: входная точка HTTP, auth (JWT), rate limit, валидация и size-лимиты, correlation id, маршрутизация на use-cases модулей.

## Документы
- [00-overview.md](00-overview.md)
- [01-context.md](01-context.md)
- [02-api-contracts.md](02-api-contracts.md)
- [03-architecture.md](03-architecture.md)
- [04-data-model.md](04-data-model.md)
- [06-rbac.md](06-rbac.md)
- [07-implementation-phases.md](07-implementation-phases.md)
- [09-testing.md](09-testing.md)

## DoD
Все endpoint защищены JWT; rate limit и size-лимиты enforced; невалидный ввод → 422; correlation id в каждом логе; маршрутизация покрыта тестами.

## Changelog
- 2026-05-21: bootstrap (architect).
- 2026-05-21: реализован (backend), тесты зелёные, ревью пройдено. Код: `src/app/api_gateway/` (auth, middleware, rate_limit, routers/). Добавлены trusted-proxy XFF parsing (`TRUSTED_PROXY_IPS`/`HOP_COUNT`), служебные маршруты `/health`/`/ready`/`/metrics`.
- 2026-05-25: спроектировано ТЗ на улучшение OpenAPI/Swagger документации (русский язык, JWT Bearer scheme, теги, blocked-ответы, примеры, `DOCS_ENABLED` для prod). Стандарт — [08-api-documentation.md](../../08-api-documentation.md). Ожидает реализации backend.
- 2026-05-25: спроектирован ленивый провижининг users (BUG-1, CRITICAL) — идемпотентный upsert в `get_current_user` после JWT-верификации, до downstream ([ADR-007](../../adr/ADR-007-lazy-user-provisioning.md)). Ожидает реализации backend.
- 2026-06-02: спроектирован `GET /healthz` — алиас `/health` (`200`, публичный, без auth) для healthcheck внешнего Traefik/smoke ([ADR-017](../../adr/ADR-017-shared-server-traefik-deploy.md), GW-8). Минимальная правка health-router + регистрация в `main.py`. Ожидает реализации backend.
