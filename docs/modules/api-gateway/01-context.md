# API Gateway — Context

## Соседи и зависимости
| Зависит от | Зачем |
|---|---|
| Redis | rate limit, size-метрики |
| JWKS endpoint | проверка подписи JWT |
| Все use-case модули | маршрутизация |
| Observability | middleware метрик/логов/трейсов |

## Кто зависит от Gateway
Все внешние вызовы iOS проходят через Gateway. Модули не вызываются напрямую извне.

## Контекст безопасности
См. [05-security.md](../../05-security.md): JWT RS256, сверка `userId == sub`, rate limits, size-лимиты, redaction секретов в логах.

## Связанные ADR
- [ADR-004](../../adr/ADR-004-blocked-http-200.md) — маппинг 200/4xx/5xx.
