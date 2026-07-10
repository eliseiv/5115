# Profile — Testing

## Unit
- `account_id(user_id)` — детерминизм (один UUID → один accountId), формат `XXXX-XXXX-XXXXX`, разные UUID → разные значения (на выборке).
- `displayName` валидация длины (≤ 80), пустая строка → `null`.

## Integration
- `GET /v1/profile` — возвращает `accountId`/`displayName`/`createdAt`; `accountId` стабилен между вызовами.
- `PATCH /v1/profile` — сохраняет имя; повторный GET отражает; «Changes saved» путь.
- Изоляция: `userId` ≠ `sub` → `403`.
