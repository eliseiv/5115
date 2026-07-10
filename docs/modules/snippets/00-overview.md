# Snippets — Overview

## Назначение
Сохранённые код-фрагменты для Code-режима: список с фильтром по языку (All/TypeScript/Python/SQL/…), поиск, сохранение из чата, контекстные действия (Open in Chat, Add to Project, Delete).

## Scope
- `GET /v1/snippets` — список (фильтр `language`, поиск `q` по title/code, пагинация).
- `POST /v1/snippets` — создать (вручную или сохранить из чата с `sourceChatId`).
- `GET /v1/snippets/{id}` — полный фрагмент.
- `PATCH /v1/snippets/{id}` — изменить title/language/code/tags.
- `DELETE /v1/snippets/{id}` — удалить (с подтверждением на клиенте).

## Out of scope
- Синтаксическая подсветка/валидация кода (клиент).
- Версионирование сниппетов.
- Шеринг между пользователями.

## Действия дизайна → backend
- **Open in Chat** — клиентское: использует `sourceChatId` (если есть) или создаёт новый чат, подставляя `code` в сообщение. Backend-эндпоинта не требует (использует `/chat/run`).
- **Add to Project** — клиентское: привязка идёт на уровне workspace (вставка кода в чат workspace). Отдельного backend-эндпоинта не требует.

## Бизнес-правила
- BR-SN-1: сниппет принадлежит `sub`; чужой → `404`.
- BR-SN-2: `language` — свободная строка с нормализацией (фильтр UI), напр. `TypeScript`/`Python`/`SQL`. Фильтр `All` = без фильтра.
- BR-SN-3: `sourceChatId` (nullable) — для «Open in Chat»; при удалении чата → SET NULL (сниппет остаётся).
- BR-SN-4: `code` ≤ 64KB, `title` ≤ 200, ≤ 20 тегов.
