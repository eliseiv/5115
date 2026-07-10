# Profile — API Contracts

Все эндпоинты — JWT, владелец = `sub`.

## GET /v1/profile
### Response (200)
```json
{
  "accountId": "8472-1936-AXQ5",
  "displayName": "string | null",
  "createdAt": "ISO8601"
}
```
- `accountId` — производная от `user_id` (BR-PR-1), стабильна.

## PATCH /v1/profile
### Request
```json
{ "displayName": "string" }
```
- `extra='forbid'`. `displayName` ≤ 80 символов; пустая строка → трактуется как сброс в `null` (или `422` — дефолт: `null`, очистка имени допустима).

### Response (200)
Та же схема, что `GET /v1/profile` (с обновлённым `displayName`). UI показывает «Changes saved».

## Ошибки
- `userId` (если присутствует в пути/теле) ≠ `sub` → `403` (сквозное правило gateway).
- Невалидная длина → `422`.
