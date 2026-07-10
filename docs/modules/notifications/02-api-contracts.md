# Notifications — API Contracts

JWT, владелец = `sub`.

## POST /v1/notifications/device-token
Регистрация/обновление APNs device-токена.

### Request
```json
{
  "deviceId": "string (optional — иначе из JWT claim / X-Device-Id)",
  "pushToken": "string (APNs device token)",
  "platform": "ios"
}
```
- `extra='forbid'`. `pushToken` ≤ 512 символов. `deviceId` — если не передан, берётся из JWT/`X-Device-Id`; если и там нет → `422` (нужен device для уникальности).
- Upsert по `(user_id, device_id)`.

### Response (200)
```json
{ "registered": true }
```

## DELETE /v1/notifications/device-token
Удалить токен устройства (отписка / logout).
### Request
```json
{ "deviceId": "string (optional — иначе из JWT/X-Device-Id)" }
```
### Response (200)
```json
{ "deleted": true }
```

## Настройка уведомлений (toggle)
- `notificationsEnabled` — через [preferences](../preferences/02-api-contracts.md): `GET`/`PATCH /v1/preferences`. Отдельного toggle-эндпоинта в notifications нет (единый источник `user_preferences`).

> Отправка push (APNs) — **не реализуется** в этом проходе ([TD-011](../../100-known-tech-debt.md)). Контракты выше покрывают только хранение токена; доставка добавится отдельным проходом.
