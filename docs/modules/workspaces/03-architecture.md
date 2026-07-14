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

> **Актуально по [ADR-064](../../adr/ADR-064-workspace-files-live-reinjection.md) (2026-07-14).** `instructions` И файлы-знания — **живой per-turn контекст**: собираются через `WorkspacesService.context_for_session` и подаются в **первый LLM-вызов каждого** generation-запроса сессии с `workspace_project_id` (turn 0 / resume `/chat/run` / первый вызов `/chat/tool-result`), **независимо от `ctx.is_new`**. Файлы **НЕ персистируются** в `chat_steps`. Прежняя формулировка ADR-036 §6 «файлы сохранены как content-блоки истории и реплеятся автоматически» — **никогда не была реализована** (прод-баг «файлы видны только на turn 0») и снята.

Композиция system-prompt — единый helper `_system_prompt_with_workspace(assistant_mode, instructions)` (orchestrator), вызываемый на каждом обращении к LLM.

**Каждый generation-запрос сессии с `workspace_project_id`** (turn 0 / resume / continuation): orchestrator запрашивает у workspaces `(instructions, files)` живого проекта владельца через `WorkspacesService.context_for_session`:
1. **`instructions` → system-prompt.** Добавляется **после** base assistant_mode prompt ([ADR-012](../../adr/ADR-012-assistant-mode-vs-billing-mode.md)). Порядок: `base(assistant_mode)` → `\n\n` → `workspace.instructions`. Пустые/`null` → инъекции нет (system-prompt = base, prompt cache не ломается). Провайдер-агностично ([ADR-033](../../adr/ADR-033-llm-provider-abstraction.md)).
2. **Файлы-знания → контекст (инъектируются в последний user-turn первого LLM-вызова запроса; НЕ персистируются).**
   - document/text (`extracted_text` непуст) → текстовый блок `[Файл проекта: {filename}]\n{extracted_text}`. Работает на обоих провайдерах ([ADR-033](../../adr/ADR-033-llm-provider-abstraction.md)) by design — это текст, не нативный PDF (inline-PDF на OpenAI отдельно поддержан [ADR-041](../../adr/ADR-041-openai-native-pdf-attachment.md)).
   - image → vision-блок (провайдер-агностично через клиент; сырой base64 в `chat_steps` не пишется — инвариант [ADR-020](../../adr/ADR-020-inline-base64-attachments-mvp.md)).
3. **Лимит** `WORKSPACE_CONTEXT_MAX_CHARS` (дефолт 200000) на суммарный инжектируемый текст; превышение → усечение `extracted_text` (порядок `created_at` ASC, старые первыми, усекается хвост; точная стратегия при росте — [Q-013-1](../../99-open-questions.md)). Изображения в лимит символов не входят.

**Расход внутри запроса:** файлы подаются в **первый** вызов `create_message` запроса; `turn0_attachments` обнуляется после него, поэтому на внутренних server-side tool-раундах того же запроса файлы не повторяются (паритет стоимости). Удалённый/чужой workspace или пустые instructions/файлы → base system-prompt без инъекции, без файлов (graceful).

**Почему и instructions, и файлы — на каждом ходе (симметрично).** `system`-prompt и user-content подаются заново на **каждый** вызов LLM. `instructions` живут в `system`; файлы — в user-content первого вызова запроса. Ни то, ни другое **не персистируется как история** файлов, поэтому оба переинъектируются на каждом generation-запросе — иначе на втором и дальше ходах проектный контекст пропал бы (это и был прод-баг для файлов). Чаты, **перенесённые** в workspace позже ([ADR-038](../../adr/ADR-038-move-chat-to-workspace.md)) или с **отредактированным первым сообщением** ([ADR-040 §4а](../../adr/ADR-040-edit-message-and-regenerate.md)), тоже получают и instructions, и файлы (развязка от `ctx.is_new`; [Q-038-1](../../99-open-questions.md)/[Q-040-3](../../99-open-questions.md) закрыты). Разведение с inline-вложениями запроса ([ADR-020](../../adr/ADR-020-inline-base64-attachments-mvp.md)): те — turn-0-only одноразовый ввод (плейсхолдер персистится); файлы workspace — живой проектный контекст на каждом ходе, без персиста.

## Привязка/изоляция
- `workspace_project_id` фиксируется на сессию при создании (orchestrator), не меняется задним числом (session-fixed как `mode`/`assistantMode`/`model`).
- Валидация принадлежности workspace пользователю при создании сессии: чужой/несуществующий → `404`.
- Все запросы скоупятся `WHERE user_id = :sub` (workspace) и `workspace_project_id` (файлы).

## Инварианты
- Workspace ≠ website-builder project ([ADR-013](../../adr/ADR-013-workspace-projects-vs-website-builder.md)); `workspace_project_id` (UUID FK) ≠ `project_id` (TEXT).
- Файлы-знания хранятся **в `workspace_files` (BYTEA)** — самодостаточно, без зависимости от отложенного `attachments` ([TD-015](../../100-known-tech-debt.md)).
- Удаление workspace: `workspace_files` CASCADE, `chat_sessions.workspace_project_id` SET NULL (чаты живут).
- Биллинг неизменен ([ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md)): CRUD/файлы бесплатно, генерация 1 кредит.
