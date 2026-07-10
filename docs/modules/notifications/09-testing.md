# Notifications — Testing

## Unit
- Резолв `deviceId` (тело → JWT → X-Device-Id → `422` при отсутствии).
- Upsert по `(user_id, device_id)` (перерегистрация обновляет токен, не плодит строки).

## Integration
- `POST /v1/notifications/device-token` — регистрация и повторная (upsert).
- `DELETE` — удаление токена устройства; повторный → идемпотентно.
- Изоляция: токен другого `sub` недоступен.
- Toggle через `PATCH /v1/preferences` (`notificationsEnabled`) — отражается в `user_preferences`.

## Out of scope (TD-011)
- Фактическая доставка push не тестируется в этом проходе (нет APNs-клиента).
