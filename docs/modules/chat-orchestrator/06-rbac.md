# Chat Orchestrator — RBAC

## Роль
- `user` — работает только со своими сессиями.

## Правила
- `userId` запроса == `sub` JWT (enforced на Gateway).
- Сессия должна принадлежать `userId`: `chat_sessions.user_id == userId`, иначе `404` (не раскрываем существование чужой сессии).
- `toolCallId` должен принадлежать сессии пользователя (`tool_calls.session_id` → `chat_sessions.user_id == userId`).
- BYOK plaintext ключ не возвращается клиенту никогда.
