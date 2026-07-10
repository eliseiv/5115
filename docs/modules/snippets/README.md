# Module: Snippets (Code-режим)

- Статус: Спроектирован (backend — Спринт 2)
- Ответственность: сохранённые код-фрагменты (title/language/code/tags), фильтр по языку, поиск, CRUD, действия «Open in Chat» / «Add to Project».

## Документы
- [00-overview.md](00-overview.md)
- [01-context.md](01-context.md)
- [02-api-contracts.md](02-api-contracts.md)
- [03-architecture.md](03-architecture.md)
- [06-rbac.md](06-rbac.md)
- [07-implementation-phases.md](07-implementation-phases.md)
- [09-testing.md](09-testing.md)

> Data model — `snippets` (таблица 15; создаётся **отдельной будущей миграцией** Спринта 2, НЕ `0004` — `0004` создаёт только `user_preferences` + поля `chat_sessions`/`users`).

## DoD
- `GET /v1/snippets` (фильтр `language`, поиск `q`, пагинация), `POST /v1/snippets` (создать/сохранить из чата), `GET /v1/snippets/{id}`, `PATCH`, `DELETE`.
- Изоляция владельца. `sourceChatId` для «Open in Chat».

## Changelog
- 2026-06-02: bootstrap модуля (architect, Figma-gap). Таблица `snippets`. См. [figma-gap-analysis.md](../../figma-gap-analysis.md).
