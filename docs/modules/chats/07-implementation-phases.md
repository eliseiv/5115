# Chats — Implementation Phases

Спринт 1 (ядро). Зависит от миграции `0004` (поля `chat_sessions.title`/`is_pinned`/`assistant_mode`).

1. **Phase 1 — миграция `0004`:** добавить `title`/`is_pinned`/`assistant_mode` в `chat_sessions` + индекс `ix_sessions_user_pinned_updated`. Enum `assistant_mode` создаётся здесь же (общий для preferences/workspaces). Колонка `chat_sessions.workspace_project_id` и индекс `ix_sessions_workspace` в Спринт 1 **НЕ создаются** — отложены на **СПРИНТ 2** вместе с модулем `workspaces` ([ADR-013](../../adr/ADR-013-workspace-projects-vs-website-builder.md)).
2. **Phase 2 — list/get:** `GET /v1/chats` (пагинация, поиск, сортировка), `GET /v1/chats/{id}` (история шагов).
3. **Phase 3 — steps-view:** `GET /v1/chats/{id}/steps`.
4. **Phase 4 — мутации:** `PATCH` (rename/pin), `DELETE`. Автоген `title` в orchestrator при создании сессии.

Зависимости: миграция `0004` (MVP) создаёт поля `chat_sessions`/`users` + таблицу `user_preferences` (общая с модулем preferences, Спринт 1). Объекты Спринта 2+ — `workspace_projects`/`snippets`/`chat_sessions.workspace_project_id` (и отложенные `workspace_files`/`attachments`/`device_push_tokens`, [TD-015](../../100-known-tech-debt.md)) — **в `0004` НЕ входят**, создаются отдельными будущими миграциями (дробление — на усмотрение devops/backend, фиксируется в 07-deployment).
