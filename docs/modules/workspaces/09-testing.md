# Workspaces — Testing

Реализация — [ADR-036](../../adr/ADR-036-workspaces-implementation.md). Стратегия — [06-testing-strategy.md](../../06-testing-strategy.md).

## Unit
- Лимиты длины (`name`/`description`/`instructions`); пустой `name` → `422`.
- Извлечение текста: PDF (pypdf) → `extracted_text`; text/json → decode; image → `extracted_text=NULL`.
- Лимиты файлов: число > `WORKSPACE_FILE_MAX_COUNT` → `422`; файл > `WORKSPACE_FILE_MAX_BYTES` → `413`; суммарно > `WORKSPACE_FILES_TOTAL_BYTES` → `422`; `mediaType` вне allowlist → `422`.
- Усечение суммарного контекста до `WORKSPACE_CONTEXT_MAX_CHARS` (порядок `created_at` ASC, усекается хвост).
- Порядок сборки system-prompt (`_system_prompt_with_workspace`): `base(assistant_mode)` → `\n\n` → `instructions`; пустые/`null` `instructions` → system-prompt = base (без инъекции). Тот же helper используется и на turn 0, и на continuation.

## Integration
- CRUD workspace; чужой/несуществующий → `404`. Курсорная пагинация списка (`nextCursor`, `fileCount`/`chatCount`).
- Файлы: загрузка своего → `201` (байты в `workspace_files.content`, `extracted_text` извлечён); список → метаданные без тела; DELETE → `200`, повторный/чужой `fileId` → `404`.
- DELETE workspace: `workspace_files` CASCADE (BYTEA удалён); `chat_sessions.workspace_project_id` → NULL (чат жив, история цела).
- `/chat/run` с `workspaceProjectId`: instructions попадают в system-prompt, document/text-файлы — в контекст (`extracted_text`), image — vision; чужой workspace → `404 workspace_not_found`. На resume сессии файлы заново не инжектируются.
- **Instructions на continuation (фикс [ADR-036 §3](../../adr/ADR-036-workspaces-implementation.md)):** `/chat/tool-result` в сессии workspace переинъектирует `instructions` в system-prompt (`system` не часть истории) на каждом continuation-витке; файлы-знания на continuation повторно НЕ подаются (уже в истории). Удалённый workspace / пустые instructions на continuation → base system-prompt (graceful).
- Провайдер-агностичность: document/text-контекст подаётся и на Anthropic, и на OpenAI как `extracted_text` (текст, не нативный PDF) — by design, независимо от пути inline-PDF (на OpenAI inline-PDF поддержан [ADR-041](../../adr/ADR-041-openai-native-pdf-attachment.md), бывш. [TD-023](../../100-known-tech-debt.md)).
- Биллинг: создание workspace/загрузка файла — ledger не пишется; генерация в чате проекта — 1 кредит ([ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md)).
- Разведение: `workspace_project_id` не путается с website-builder `project_id` (разные сессии/поля/таблицы).

## Изоляция
- Все эндпоинты скоупятся `sub`; файлы — `workspace_project_id` владельца.
