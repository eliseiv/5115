# Workspaces — Architecture

Реализация — [ADR-036](../../adr/ADR-036-workspaces-implementation.md).

## Размещение
Пакет `src/app/workspaces/`:
- `repository.py` — запросы над `workspace_projects`/`workspace_files` (всё скоупится `WHERE user_id = :sub`).
- `service.py` — use-cases: CRUD workspace, upload/list/delete файлов (с извлечением `extracted_text`), сборка контекста для orchestrator.
- `text_extract.py` (или reuse `chat/attachments.py`) — извлечение текста: `pypdf` для PDF, decode для `text/*`+`json`.
- Роутер `/v1/workspaces/*` в `src/app/api_gateway/routers/workspaces.py`.
- Схемы в `src/app/schemas/workspaces.py`.

## Хранение файлов-знаний (BYTEA, образец `site_files`)
- `workspace_files.content` (BYTEA) — сырые байты файла; `extracted_text` (Text, nullable) — извлечённый текст (document/text) или NULL (image).
- Загрузка: inline base64 → декод → валидация (allowlist/размер/число, reuse `attachments.py`) → извлечение текста → INSERT (`content`, `extracted_text`, `media_type`, `size`, `filename`).
- API тело файла наружу не отдаёт; `content`/`extracted_text` читаются только при подаче контекста модели.
- Object storage — отложено ([TD-027](../../100-known-tech-debt.md), как [TD-009](../../100-known-tech-debt.md) для `site_files`).

## Подача контекста модели (вызывается orchestrator)
Композиция system-prompt — единый helper `_system_prompt_with_workspace(assistant_mode, instructions)` (orchestrator), вызываемый и на turn 0, и на каждом continuation-витке.

**Turn 0 (новая сессия с `workspace_project_id`):** orchestrator запрашивает у workspaces `(instructions, files)` проекта владельца через `WorkspacesService.context_for_session`:
1. **`instructions` → system-prompt.** Добавляется **после** base assistant_mode prompt ([ADR-012](../../adr/ADR-012-assistant-mode-vs-billing-mode.md)). Порядок: `base(assistant_mode)` → `\n\n` → `workspace.instructions`. Пустые/`null` → инъекции нет (system-prompt = base, prompt cache не ломается). Провайдер-агностично ([ADR-033](../../adr/ADR-033-llm-provider-abstraction.md)).
2. **Файлы-знания → контекст первого сообщения.**
   - document/text (`extracted_text` непуст) → текстовый блок `[Файл проекта: {filename}]\n{extracted_text}`. Работает на обоих провайдерах ([ADR-033](../../adr/ADR-033-llm-provider-abstraction.md)) by design — это текст, не нативный PDF (inline-PDF на OpenAI отдельно поддержан [ADR-041](../../adr/ADR-041-openai-native-pdf-attachment.md)).
   - image → vision-блок (механика общая с inline-attachments turn-0, провайдер-агностично через клиент).
3. **Лимит** `WORKSPACE_CONTEXT_MAX_CHARS` (дефолт 200000) на суммарный инжектируемый текст; превышение → усечение `extracted_text` (порядок `created_at` ASC, старые первыми, усекается хвост; точная стратегия при росте — [Q-013-1](../../99-open-questions.md)). Изображения в лимит символов не входят.

**Continuation (tool-loop `/chat/tool-result`):** orchestrator переинъектирует ТОЛЬКО `instructions` через `WorkspacesService.instructions_for_session` (лёгкое single-column чтение `WorkspacesRepository.get_instructions`), тем же helper'ом `_system_prompt_with_workspace`. Удалённый/чужой workspace или пустые instructions → base system-prompt без инъекции (graceful).

**Почему instructions — на каждом ходе, а файлы — только на turn 0.** `system`-prompt передаётся отдельным параметром `system` на **каждый** вызов LLM и **не является частью истории сообщений** — поэтому `instructions` нужно подавать заново на turn 0 И на каждом continuation-витке, иначе на втором и дальше витках tool-loop кастомный system-prompt проекта пропадёт. Файлы-знания, наоборот, сохранены как content-блоки **в истории сообщений** после turn 0 — они автоматически реплеятся в каждом последующем запросе и повторно не подаются (как inline-attachments turn-0). На resume существующей сессии файлы заново не инжектируются (контекст уже в первых шагах истории); instructions при resume также проходят через тот же helper на каждом ходе.

## Привязка/изоляция
- `workspace_project_id` фиксируется на сессию при создании (orchestrator), не меняется задним числом (session-fixed как `mode`/`assistantMode`/`model`).
- Валидация принадлежности workspace пользователю при создании сессии: чужой/несуществующий → `404`.
- Все запросы скоупятся `WHERE user_id = :sub` (workspace) и `workspace_project_id` (файлы).

## Инварианты
- Workspace ≠ website-builder project ([ADR-013](../../adr/ADR-013-workspace-projects-vs-website-builder.md)); `workspace_project_id` (UUID FK) ≠ `project_id` (TEXT).
- Файлы-знания хранятся **в `workspace_files` (BYTEA)** — самодостаточно, без зависимости от отложенного `attachments` ([TD-015](../../100-known-tech-debt.md)).
- Удаление workspace: `workspace_files` CASCADE, `chat_sessions.workspace_project_id` SET NULL (чаты живут).
- Биллинг неизменен ([ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md)): CRUD/файлы бесплатно, генерация 1 кредит.
