# Module: Workspaces (рабочие пространства чатов)

- Статус: **Реализован (Поставка 3, [ADR-036](../../adr/ADR-036-workspaces-implementation.md))** — код в `src/app/workspaces/` + роутер + миграция `0011`; offline-сьют зелёный (1286 passed); расширяет [ADR-013](../../adr/ADR-013-workspace-projects-vs-website-builder.md).
- Ответственность: проекты-воркспейсы = `name` + `description` + кастомные `instructions` (system-prompt проекта) + файлы-знания (контекст для всех чатов проекта) + список/группировка чатов проекта. **НЕ путать** с website-builder `projects` ([ADR-013](../../adr/ADR-013-workspace-projects-vs-website-builder.md)).

## Документы
- [00-overview.md](00-overview.md)
- [01-context.md](01-context.md)
- [02-api-contracts.md](02-api-contracts.md)
- [03-architecture.md](03-architecture.md)
- [06-rbac.md](06-rbac.md)
- [07-implementation-phases.md](07-implementation-phases.md)
- [09-testing.md](09-testing.md)

> **Data model ([ADR-036](../../adr/ADR-036-workspaces-implementation.md)):** `workspace_projects` ([03-data-model §13](../../03-data-model.md)) + `chat_sessions.workspace_project_id` (nullable FK, ON DELETE SET NULL) + **`workspace_files` ([03-data-model §14](../../03-data-model.md)) — собственное BYTEA-хранение** (образец `site_files`, [TD-027](../../100-known-tech-debt.md)), **НЕ через `attachments`**. Создаются миграцией **`0011`** (цепочка `0001`→`0011`, expand-only). Файлы-знания **самодостаточны** — фича не зависит от отложенного модуля `attachments` ([TD-015](../../100-known-tech-debt.md)).

## DoD
- **Под-фаза 3A (ядро):** CRUD `/v1/workspaces` (name/description/instructions, курсорная пагинация); изоляция по `sub` (`404`); `ChatRunRequest.workspaceProjectId` (session-fixed, валидация принадлежности); реальный `workspaceProjectId` в списке чатов + фильтр `GET /v1/chats?workspaceProjectId=`; инъекция `instructions` в system-prompt после base assistant_mode prompt; удаление workspace → файлы CASCADE, чаты SET NULL.
- **Под-фаза 3B (файлы-знания):** `POST/GET/DELETE /v1/workspaces/{id}/files[/{fileId}]` (inline base64, BYTEA-хранение, извлечение `extracted_text`, лимиты); инъекция файлов в чаты workspace (document/text → `extracted_text`, image → vision; лимит `WORKSPACE_CONTEXT_MAX_CHARS`).

## Changelog
- 2026-06-25 ([ADR-045](../../adr/ADR-045-per-path-body-limit-workspace-files.md), docs-only — ТЗ для backend): **фикс 413 на загрузке файлов**. `POST /v1/workspaces/{id}/files` резался общим 512 KB (`SIZE_LIMIT_BODY`) в gateway **до** валидатора `validate_and_extract` → заявленный `WORKSPACE_FILE_MAX_BYTES`=8 MB был недостижим (реальный потолок ~375 KB). Решение: новый конфиг `WORKSPACE_REQUEST_BODY_LIMIT` (дефолт 12 MB) + правило матча пути `startswith("/v1/workspaces/") and endswith("/files")` в `SizeLimitMiddleware._limit_for` (матчит только этот POST; CRUD/`{file_id}`-delete сохраняют 512 KB). Инвариант: `WORKSPACE_REQUEST_BODY_LIMIT ≥ WORKSPACE_FILE_MAX_BYTES*4/3 + JSON-запас`. Файлы backend: `config.py`, `api_gateway/middleware.py`. Без миграции, контракт не меняется. См. [02-api-contracts.md §POST …/files](02-api-contracts.md#post-v1workspacesworkspace_idfiles). Scope backend + qa.
- 2026-06-18 ([ADR-038](../../adr/ADR-038-move-chat-to-workspace.md), docs-only — код не написан): **перенос существующего чата в воркспейс**. `chat_sessions.workspace_project_id` становится изменяемым через `PATCH /v1/chats/{id}` (`workspaceProjectId: uuid|null`, модуль [chats](../chats/README.md)); целевой workspace валидируется тем же `owns_workspace` (уже есть в `WorkspacesService`) → чужой `404 workspace_not_found`. **Изменение инъекции:** `instructions` подаются в system-prompt на каждом ходе сессии с workspace (включая чаты, перенесённые позже) — orchestrator развязывает инъекцию instructions от `ctx.is_new`; **файлы-знания остаются turn-0-only** (не реинъектируются ретроспективно, [Q-038-1](../../99-open-questions.md)). Без миграции (колонка из `0011`). См. [02-api-contracts.md §Привязка чатов](02-api-contracts.md#привязка-чатов). Scope backend + qa.
- 2026-06-17: **РЕАЛИЗОВАН** (Поставка 3, [ADR-036](../../adr/ADR-036-workspaces-implementation.md)) — backend `src/app/workspaces/` + роутер + миграция `0011` в репозитории; backend approve, offline-сьют 1286 passed. Финальный sync статус-маркеров (README модуля + таблица модулей [docs/README.md](../../README.md)).
- 2026-06-17: **переписан под [ADR-036](../../adr/ADR-036-workspaces-implementation.md)** (Поставка 3): файлы-знания переведены на собственную таблицу `workspace_files` (BYTEA, inline base64, извлечение текста) — **снята зависимость от отложенного `attachments`** ([TD-015](../../100-known-tech-debt.md)); зафиксированы API-путь (`/v1/workspaces`), инъекция instructions/файлов, удаление (файлы CASCADE / чаты SET NULL), лимиты, пагинация, биллинг. Миграция `0011`.
- 2026-06-02: bootstrap модуля (architect, Figma-gap). [ADR-013](../../adr/ADR-013-workspace-projects-vs-website-builder.md).
