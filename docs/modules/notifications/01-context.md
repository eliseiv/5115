# Notifications — Context

## Зависимости
- **API Gateway** — auth, provisioning, `device_id` из claim/`X-Device-Id`, роуты `/v1/notifications/*`.
- **preferences** — `notifications_enabled` (единый источник настройки). Notifications-модуль не дублирует toggle.
- **device_push_tokens** таблица.

## Будущие зависимости (TD-011)
- **APNs** (Apple Push Notification service) — token-based JWT auth, env `APNS_*`. Не подключается в этом проходе.

## Границы
- На старте модуль — только хранилище токенов + чтение настройки. Не отправляет push, не имеет фоновых джобов.
