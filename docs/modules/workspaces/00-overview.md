# Workspaces — Overview

## Назначение
Рабочее пространство чатов (дизайн «Project»): контейнер с именем, описанием, кастомными инструкциями (system-prompt проекта) и файлами-знаниями (PDF/текст/изображения), внутри которого ведутся чаты с общим контекстом. Реализация — [ADR-036](../../adr/ADR-036-workspaces-implementation.md) (расширяет [ADR-013](../../adr/ADR-013-workspace-projects-vs-website-builder.md)).

## Разведение с website-builder ([ADR-013](../../adr/ADR-013-workspace-projects-vs-website-builder.md))
- **workspace** (этот модуль) — **вход/контейнер**: контекст + инструкции для модели, группировка чатов.
- **website-builder `projects`** — **вывод/артефакт**: сгенерированные статические сайты (`site_files`, server-side `site.*`). Другая сущность, не переиспользуется.
- API-путь — **`/v1/workspaces`** (НЕ `/v1/projects` — слово «project» в API занято website-builder). iOS отображает «Projects», обращается к `/v1/workspaces` ([ADR-036 §1](../../adr/ADR-036-workspaces-implementation.md)).
- `chat_sessions.workspace_project_id` (UUID FK) ≠ `chat_sessions.project_id` (TEXT, website-builder external id). Разные поля.

## Scope
- CRUD workspace: `POST/GET/PATCH/DELETE /v1/workspaces[/{id}]` (курсорная пагинация списка как chats).
- Управление `instructions` — через PATCH workspace (`instructions` — поле, кастомный system-prompt).
- Файлы-знания: `POST /v1/workspaces/{id}/files` (inline base64, BYTEA-хранение), `GET` (список), `DELETE /v1/workspaces/{id}/files/{fileId}`.
- Привязка чатов: при `/chat/run` с `workspaceProjectId` (session-fixed); список чатов workspace — `GET /v1/chats?workspaceProjectId=...` (модуль chats).
- Подача `instructions` (system-prompt) + файлов-знаний (контекст) модели при генерации в сессии workspace.

## Out of scope
- Совместный доступ/шеринг workspace между пользователями.
- Версионирование инструкций/файлов.
- RAG/векторный поиск по файлам (на старте — вставка `extracted_text` с лимитом, [Q-013-1](../../99-open-questions.md)).
- Object storage для файлов (на старте — BYTEA в БД, [TD-027](../../100-known-tech-debt.md)).

## Бизнес-правила
- BR-WS-1: workspace принадлежит пользователю (`user_id == sub`); чужой/несуществующий → `404` (никогда не раскрывать чужое существование).
- BR-WS-2: `instructions` (nullable) добавляются к base-system-prompt **после** assistant_mode prompt ([ADR-012](../../adr/ADR-012-assistant-mode-vs-billing-mode.md)) при генерации в сессии этого workspace, на **КАЖДОМ ходе** — turn 0 И continuation (tool-loop): `system`-prompt не часть истории, instructions переинъектируются на каждый вызов LLM ([ADR-036 §3](../../adr/ADR-036-workspaces-implementation.md), helper `_system_prompt_with_workspace`). Пустые → инъекции нет (prompt cache не ломается).
- BR-WS-3: файл-знание хранится **в собственной таблице `workspace_files`** (BYTEA, [ADR-036 §4](../../adr/ADR-036-workspaces-implementation.md)), **не** через `attachments`. Загрузка — inline base64 (reuse валидаций [ADR-020](../../adr/ADR-020-inline-base64-attachments-mvp.md)); при загрузке извлекается `extracted_text` (pypdf для PDF, decode для текста). Файл принадлежит workspace (значит — пользователю); лимиты числа/размера/типа — [ADR-036 §4](../../adr/ADR-036-workspaces-implementation.md).
- BR-WS-4: подача файлов-знаний модели — document/text → `extracted_text` (текстовый контекст, работает на **обоих** провайдерах [ADR-033](../../adr/ADR-033-llm-provider-abstraction.md) by design — это извлечённый текст, не нативный PDF; inline-PDF на OpenAI отдельно поддержан [ADR-041](../../adr/ADR-041-openai-native-pdf-attachment.md)); image → vision-блок. Суммарный размер инжектируемого текста ограничен `WORKSPACE_CONTEXT_MAX_CHARS` (усечение, [Q-013-1](../../99-open-questions.md)).
- BR-WS-5: удаление workspace → `workspace_files` **CASCADE** (BYTEA удаляется); `chat_sessions.workspace_project_id` → **SET NULL** (чаты сохраняются как «чистые», история не теряется).
- BR-WS-6: биллинг неизменен ([ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md)): создание workspace и загрузка/удаление файлов — бесплатно (CRUD); генерация в чате проекта — 1 кредит = 1 сообщение.
