# Module: Profile

- Статус: Реализован (Спринт 1)
- Ответственность: профиль пользователя — `displayName` (редактируемое имя) + производный человекочитаемый `accountId`. Экран Profile (Save Changes / Changes saved).

## Документы
- [00-overview.md](00-overview.md)
- [01-context.md](01-context.md)
- [02-api-contracts.md](02-api-contracts.md)
- [03-architecture.md](03-architecture.md)
- [06-rbac.md](06-rbac.md)
- [07-implementation-phases.md](07-implementation-phases.md)
- [09-testing.md](09-testing.md)

> Data model — `users.display_name` (миграция `0004`, общий [03-data-model.md](../../03-data-model.md)). `accountId` НЕ хранится (производная от `user_id`).

## DoD
- `GET /v1/profile` (возвращает `displayName`, производный `accountId`, `createdAt`), `PATCH /v1/profile` (изменить `displayName`).
- `accountId` — детерминированная производная от `user_id` в формате `XXXX-XXXX-XXXXX` (две 4-значные цифровые группы + 5-символьная alphanumeric группа из безошибочного алфавита `ABCDEFGHJKLMNPQRSTUVWXYZ23456789`, напр. `8472-1936-AXQ5K`), стабилен между запросами.
- Изоляция владельца (`sub`).

## Changelog
- 2026-06-02: bootstrap модуля (architect, Figma-gap). Добавлено `users.display_name`. См. [figma-gap-analysis.md](../../figma-gap-analysis.md).
- 2026-06-02 (Спринт 1, backend): реализованы `GET /v1/profile` и `PATCH /v1/profile` (`displayName` ≤ 80 символов, пустая строка → null). `accountId` — чистая производная от `user_id` (`src/app/profile/account_id.py`), не хранится. Миграция `0004` (`users.display_name`). Тесты зелёные (offline-сьют 681/681).
