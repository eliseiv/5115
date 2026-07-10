# API Gateway — Overview

## Scope
- Терминирование HTTP-запросов от iOS, единый набор middleware.
- JWT-аутентификация (RS256), извлечение `sub` (userId), `device_id`.
- Ленивый провижининг `users` по `sub` (идемпотентный upsert после верификации JWT, до downstream) — [ADR-007](../../adr/ADR-007-lazy-user-provisioning.md).
- Rate limiting per user / device / IP (Redis).
- Валидация запросов (Pydantic v2, `extra=forbid`) и size-лимиты (до парсинга тела).
- Correlation id (`requestId`) генерация/проброс; добавление `sessionId` в контекст логов.
- Маршрутизация на use-cases модулей (chat, policy, wallet, subscription, byok).
- Стандартизация ответов и маппинг ошибок (4xx/5xx vs бизнес-200, см. ADR-004).
- Health/ready/metrics endpoints.

## Out of scope
- Бизнес-логика доступа (Policy Engine), генерация (Orchestrator), биллинг (Wallet).
- Хранение состояния (кроме Redis-метрик/лимитов). Единственное исключение записи в PostgreSQL на gateway-уровне — идемпотентный provisioning-upsert `users` ([ADR-007](../../adr/ADR-007-lazy-user-provisioning.md)); он не несёт бизнес-логики (только обеспечение FK-родителя).

## Endpoints (маршрутизирует)
Все `/v1/*` из ТЗ §4 + `/health`, `/healthz` (алиас `/health`, [ADR-017](../../adr/ADR-017-shared-server-traefik-deploy.md)), `/ready`, `/metrics`.
