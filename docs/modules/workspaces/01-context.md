# Workspaces — Context

Реализация — [ADR-036](../../adr/ADR-036-workspaces-implementation.md).

## Зависимости
- **API Gateway** — auth (JWT), provisioning, роуты `/v1/workspaces/*`.
- **chat/attachments.py** ([ADR-020](../../adr/ADR-020-inline-base64-attachments-mvp.md)) — переиспользуются классы вложений (`type`/`mediaType`/`filename`/`data`), валидации (allowlist/размер) и извлечение текста (pypdf/decode) для загрузки файлов-знаний. **Таблица `attachments` НЕ используется** (отложена, [TD-015](../../100-known-tech-debt.md)).
- Таблицы **`workspace_projects`** ([03-data-model §13](../../03-data-model.md)) / **`workspace_files`** ([§14](../../03-data-model.md), собственное BYTEA-хранение) + колонка `chat_sessions.workspace_project_id` (миграция `0011`).

## Потребители
- **chat-orchestrator** — при `/chat/run` с `workspaceProjectId` подмешивает `instructions` в system-prompt (после base assistant_mode prompt) и файлы-знания в контекст ([ADR-036 §3,§6](../../adr/ADR-036-workspaces-implementation.md)).
- **chats** — реальный `workspaceProjectId` в списке + фильтр `GET /v1/chats?workspaceProjectId=`.

## Соседи (важно не путать)
- **website-builder** — отдельная сущность `projects`/`site_files` ([ADR-013](../../adr/ADR-013-workspace-projects-vs-website-builder.md)). Workspace ≠ website-builder project; `/v1/workspaces` ≠ website-builder `project_id`.

## Границы
- Workspaces не вызывает LLM; предоставляет orchestrator данные контекста (instructions + файлы) по запросу.
- Файлы-знания хранятся в собственной таблице `workspace_files` (BYTEA, образец `site_files`) — не зависит от `attachments`.
