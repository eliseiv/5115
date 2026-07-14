# Workspaces — Implementation Phases

Поставка 3 (крупнейшая), 2 под-фазы — [ADR-036](../../adr/ADR-036-workspaces-implementation.md). Миграция **`0011`** (цепочка `0001`→`0011`, expand-only). Файлы-знания **самодостаточны** (собственная таблица `workspace_files` BYTEA) — **не** зависят от отложенного `attachments` ([TD-015](../../100-known-tech-debt.md)).

## Под-фаза 3A — ядро

1. **Миграция `0011`:**
   - `CREATE TABLE workspace_projects` (13): `id` uuid PK, `user_id` FK users CASCADE, `name` Text NOT NULL, `description` Text NULL, `instructions` Text NULL, `created_at`/`updated_at`. Индекс `ix_workspace_projects_user_updated (user_id, updated_at)`.
   - `ALTER TABLE chat_sessions ADD COLUMN workspace_project_id uuid NULL` FK `workspace_projects.id` **ON DELETE SET NULL**. Индекс `ix_sessions_workspace (workspace_project_id)` (для фильтра списка чатов).
2. **CRUD workspace:** `POST/GET/PATCH/DELETE /v1/workspaces[/{id}]` (repository + service + router + схемы). Курсорная пагинация списка (как chats). Изоляция `sub` → `404`. DELETE: чаты SET NULL (через FK), `workspace_files` — на этой фазе таблицы ещё нет (CASCADE появляется в 3B; до 3B удалять нечего).
3. **Чат-привязка (ядро):** `ChatRunRequest.workspaceProjectId: uuid|None` (session-fixed); валидация принадлежности при создании сессии → `404`; `chat_sessions.workspace_project_id` пишется при создании. Реальный `workspaceProjectId` в `ChatListItemSchema` (вместо заглушки-null в `chats/service.py`); фильтр `GET /v1/chats?workspaceProjectId=`.
4. **Инъекция instructions ([ADR-064](../../adr/ADR-064-workspace-files-live-reinjection.md)):** orchestrator подмешивает `workspace.instructions` в system-prompt **после** base assistant_mode prompt — на **КАЖДОМ ходе** (turn 0 / resume `/chat/run` / первый вызов `/chat/tool-result`; `system` не часть истории), единым helper'ом `_system_prompt_with_workspace`. Контекст на каждом ходе собирается через `context_for_session` (instructions + файлы вместе, см. п.7); отдельный `instructions_for_session` **удалён** (по [ADR-064](../../adr/ADR-064-workspace-files-live-reinjection.md) файлы тоже подаются на каждом ходе, поэтому единый сборщик).

> 3A реализуема и тестируема независимо от 3B (CRUD + привязка + инъекция instructions, без файлов).

## Под-фаза 3B — файлы-знания

5. **Миграция (часть `0011` или отдельная `0012` — на усмотрение backend, рекомендуется единая `0011`):**
   - `CREATE TABLE workspace_files` (14): `id` uuid PK, `workspace_project_id` FK `workspace_projects.id` **ON DELETE CASCADE**, `filename` Text NOT NULL, `content` BYTEA NOT NULL, `media_type` Text NOT NULL, `size` BIGINT NOT NULL CHECK (`>= 0`), `extracted_text` Text NULL, `created_at`/`updated_at`. Индекс `ix_workspace_files_project (workspace_project_id)`.
6. **API файлов:** `POST/GET/DELETE /v1/workspaces/{id}/files[/{fileId}]` (inline base64, reuse валидаций/извлечения текста `attachments.py`). Лимиты `WORKSPACE_FILE_MAX_COUNT`/`WORKSPACE_FILE_MAX_BYTES`/`WORKSPACE_FILES_TOTAL_BYTES`. Извлечение `extracted_text` при загрузке (pypdf/decode).
7. **Инъекция файлов в orchestrator ([ADR-064](../../adr/ADR-064-workspace-files-live-reinjection.md)):** при генерации в сессии workspace подмешивает файлы в **первый LLM-вызов каждого** запроса (turn 0 / resume / continuation, развязано от `ctx.is_new`; через `context_for_session`): document/text → `extracted_text` (текстовый блок), image → vision. Файлы **НЕ персистируются** в `chat_steps` (инвариант base64 [ADR-020](../../adr/ADR-020-inline-base64-attachments-mvp.md)); `turn0_attachments` расходуется после первого вызова. Лимит `WORKSPACE_CONTEXT_MAX_CHARS` (усечение).

> Если 3B выкатывается отдельной миграцией от 3A — допустимо; но рекомендуется собрать `workspace_projects` + `chat_sessions.workspace_project_id` + `workspace_files` в одной миграции `0011`, т.к. фича самодостаточна и не ждёт внешних предпосылок.
