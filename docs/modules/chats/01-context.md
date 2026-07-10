# Chats — Context

## Зависимости
- **API Gateway** — auth (JWT), lazy provisioning, rate limit, валидация. Размещает роуты `/v1/chats/*`.
- **chat-orchestrator** — владеет записью в `chat_sessions`/`chat_steps`/`tool_calls` на пути генерации. Chats читает эти таблицы и обновляет `title`/`is_pinned`/`updated_at`. Автоген `title` выполняется orchestrator при создании сессии (или chats-слоем по первому сообщению) — единый источник, без гонки.
- **workspaces** ([ADR-013](../../adr/ADR-013-workspace-projects-vs-website-builder.md)) — **СПРИНТ 2 (отложено)**: фильтр списка чатов по `workspace_project_id` (чаты проекта). В Спринте 1 модуля `workspaces`, колонки `chat_sessions.workspace_project_id` и фильтра ещё нет.

## Соседи
- **attachments** — `attachments.session_id` ссылается на чат; при удалении чата → SET NULL.

## Границы
- Chats **не** вызывает Anthropic и **не** мутирует billing/policy. Только чтение истории + метаданные чата (`title`/`is_pinned`).
- Запись `chat_steps`/`tool_calls` — исключительно orchestrator (инвариант не меняется).
