# Profile — Overview

## Назначение
Экран Profile: редактируемое отображаемое имя (`displayName`) и человекочитаемый идентификатор аккаунта (`accountId`).

## Scope
- `GET /v1/profile` — `displayName` (nullable), `accountId` (производный), `createdAt`.
- `PATCH /v1/profile` — изменить `displayName` (Save Changes → Changes saved).

## Out of scope
- Аватары/фото профиля.
- Смена email/идентичности (источник идентичности — JWT issuer, [ADR-007](../../adr/ADR-007-lazy-user-provisioning.md); регистрации нет).
- Удаление аккаунта (отдельная фича, не в этом проходе).

## Бизнес-правила
- BR-PR-1: `accountId` — **детерминированная производная** от `user_id` (UUID), формат `XXXX-XXXX-XXXXX`. Не хранится в БД, вычисляется на лету; стабилен для одного пользователя.
- BR-PR-2: `displayName` — свободная строка ≤ 80 символов, nullable (до первого сохранения — `null`).
- BR-PR-3: профиль скоупится `sub`; редактировать чужой профиль нельзя (`userId` тела/пути = `sub`).
