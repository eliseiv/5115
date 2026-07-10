# Chats — Overview

## Назначение
CRUD и просмотр истории чатов для экранов Home/история. Сейчас есть `chat_sessions`/`chat_steps`/`tool_calls`, но нет API для списка/поиска/rename/delete/steps-view. Модуль закрывает этот пробел, **не дублируя** оркестрацию (`/chat/run` остаётся в chat-orchestrator).

## Scope (этот проход)
- Список чатов: `GET /v1/chats` — `title`, `preview` (срез последнего сообщения), `updatedAt`, `isPinned`, `assistantMode`, пагинация (cursor по `updated_at`), поиск `q`.
- История: `GET /v1/chats/{id}` — упорядоченные `chat_steps` (роль/payload/usage/createdAt).
- Steps-view: `GET /v1/chats/{id}/steps` — агрегированные шаги последнего message-шага (tool-calls/reasoning, «N steps») для UI.
- Мутации: `PATCH /v1/chats/{id}` (rename `title`, `isPinned`), `DELETE /v1/chats/{id}` (cascade удаляет steps/tool_calls по FK).
- Автогенерация `title` при создании сессии в `/chat/run` (из первого user-сообщения, усечение до N символов).

## Out of scope
- Сама генерация (chat-orchestrator).
- Полнотекстовый поиск (на старте ILIKE; GIN-индекс — TD при росте).
- Экспорт/шеринг истории.

## Бизнес-правила
- BR-CH-1: доступ только владельца (`chat_sessions.user_id == sub`), иначе `404` (не раскрываем существование чужого).
- BR-CH-2: `title` автогенерируется из первого user-сообщения, если не задан; `rename` через PATCH перезаписывает.
- BR-CH-3: сортировка списка — `is_pinned DESC, updated_at DESC`.
- BR-CH-4: удаление чата каскадно удаляет `chat_steps`/`tool_calls` (FK `ON DELETE CASCADE`); `attachments.session_id` → NULL (SET NULL), вложения не удаляются вместе с чатом (принадлежат пользователю).
