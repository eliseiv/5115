# Module: Preferences

- Статус: Реализован (Спринт 1)
- Ответственность: пользовательские настройки — `defaultAssistantMode` (chat|code), `notificationsEnabled`, дефолты Code-context (`codeDefaults`). Источник дефолта `assistantMode` для `/chat/run` ([ADR-012](../../adr/ADR-012-assistant-mode-vs-billing-mode.md)).

## Документы
- [00-overview.md](00-overview.md)
- [01-context.md](01-context.md)
- [02-api-contracts.md](02-api-contracts.md)
- [03-architecture.md](03-architecture.md)
- [06-rbac.md](06-rbac.md)
- [07-implementation-phases.md](07-implementation-phases.md)
- [09-testing.md](09-testing.md)

> Data model — `user_preferences` (таблица 12, миграция `0004`, общий [03-data-model.md](../../03-data-model.md)).

## DoD
- `GET /v1/preferences` / `PATCH /v1/preferences`. Дефолты при отсутствии строки.
- `defaultAssistantMode` используется orchestrator как fallback для `assistantMode` ([ADR-012](../../adr/ADR-012-assistant-mode-vs-billing-mode.md)).
- `notificationsEnabled` — единый источник настройки уведомлений (push-токены — модуль notifications).

## Changelog
- 2026-06-02: bootstrap модуля (architect, Figma-gap). Таблица `user_preferences`. [ADR-012](../../adr/ADR-012-assistant-mode-vs-billing-mode.md). См. [figma-gap-analysis.md](../../figma-gap-analysis.md).
- 2026-06-02 (Спринт 1, backend): реализованы `GET /v1/preferences` (дефолты `chat`/`true`/`{}` при отсутствии строки) и `PATCH /v1/preferences` (частичное обновление + upsert; `defaultAssistantMode` chat|code, `notificationsEnabled`, `codeDefaults` ≤ 8 KB, без секретов). orchestrator использует `defaultAssistantMode` как fallback для `assistantMode`. Миграция `0004` (таблица `user_preferences`). Тесты зелёные (offline-сьют 681/681).
- 2026-06-16 (architect): смена контрактного дефолта `notificationsEnabled` `true` → `false` ([ADR-032](../../adr/ADR-032-notifications-enabled-default-false.md)) — privacy-by-default, iOS запрашивает системное разрешение на push сначала. Меняется только дефолт для новых/без-строки пользователей (сервисный `_defaults()` + `server_default` колонки); существующие строки `user_preferences` НЕ трогаются (миграция без backfill). Указания backend: см. ADR-032.
