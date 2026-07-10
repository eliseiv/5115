# Snippets — Context

## Зависимости
- **API Gateway** — auth, provisioning, роуты `/v1/snippets/*`.
- **snippets** таблица. `source_chat_id` → `chat_sessions` (SET NULL при удалении чата).

## Соседи
- **chats** — «Open in Chat» использует `sourceChatId`/`/chat/run` (клиентская оркестрация, без нового backend-эндпоинта).
- **workspaces** — «Add to Project» — клиентское действие через чат workspace.

## Границы
- Snippets — самостоятельное CRUD-хранилище; не вызывает Anthropic, не трогает billing/policy.
