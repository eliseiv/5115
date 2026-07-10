# Module: Notifications

- Статус: Спроектирован частично (Спринт 3) — на старте только хранение настройки + регистрация device-токена. Фактическая отправка push (APNs) — [TD-011](../../100-known-tech-debt.md).
- Ответственность: toggle уведомлений (хранится в `user_preferences.notifications_enabled`) + регистрация APNs device-токена (`device_push_tokens`).

## Документы
- [00-overview.md](00-overview.md)
- [01-context.md](01-context.md)
- [02-api-contracts.md](02-api-contracts.md)
- [03-architecture.md](03-architecture.md)
- [06-rbac.md](06-rbac.md)
- [07-implementation-phases.md](07-implementation-phases.md)
- [09-testing.md](09-testing.md)

> Data model — `device_push_tokens` (таблица 17); настройка — `user_preferences.notifications_enabled` (таблица 12, модуль preferences).

## DoD
- `POST /v1/notifications/device-token` (регистрация/обновление APNs-токена), `DELETE /v1/notifications/device-token` (отписка устройства).
- Toggle уведомлений — через `PATCH /v1/preferences` (`notificationsEnabled`).
- **Отправка push — out of scope этого прохода** ([TD-011](../../100-known-tech-debt.md)): только хранение настройки и токена.

## Changelog
- 2026-06-02: bootstrap модуля (architect, Figma-gap). Таблица `device_push_tokens`. Отправка push отложена в [TD-011](../../100-known-tech-debt.md). См. [figma-gap-analysis.md](../../figma-gap-analysis.md).
