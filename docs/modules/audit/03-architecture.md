# Audit — Architecture

## Запись
- `record(event)` → INSERT в `audit_logs`. Только INSERT, никогда UPDATE/DELETE.
- Для критичных событий (billing_debit, tool_mutation) запись участвует в той же БД-транзакции, что и действие, либо выполняется сразу после успешной фиксации — чтобы не было «действие есть, аудита нет».
- Correlation id (`requestId`, `sessionId`) включается в `payload`/контекст логов.

## Redaction
- Перед INSERT `payload` пропускается через redactor (allowlist/denylist) — гарантия отсутствия секретов.

## Транзакционность tool_mutation
Запись `tool_mutation` имеет две ветки в зависимости от того, где исполняется мутирующий tool:

1. **client-side mutating** (`files.write`/`files.mkdir`, `calendar.create_events`, `reminders.create`) — `tool_mutation` записывается при обработке `/chat/tool-result` после подтверждённого клиентом результата (`tool_calls.status=completed`), чтобы фиксировать фактически выполненное на устройстве действие.
2. **server-side mutating** (`site.write_file`, `site.delete`; см. [ADR-011](../../adr/ADR-011-server-side-tools.md)) — исполняются backend-хэндлером синхронно в tool-loop и через `/chat/tool-result` НЕ проходят. Поэтому `tool_mutation` пишется в момент синхронного исполнения хэндлера, в ТОЙ ЖЕ БД-транзакции, что и мутация `site_files` (без зависимости от `/chat/tool-result`). Гарантия: нет мутации `site_files` без соответствующего аудита.

## Append-only enforcement
- На уровне приложения: репозиторий Audit предоставляет только `insert`/`query`, без update/delete.
- БД-уровень (REVOKE UPDATE/DELETE) — отложено, [TD-001](../../100-known-tech-debt.md).
