# Chats — RBAC

- Роль `user`. Все операции ограничены ресурсами `sub` (`chat_sessions.user_id == sub`).
- Доступ к чужому чату → `404` (не раскрываем существование).
- Нет admin-операций в этом модуле.
- Никаких секретов в ответах (steps-view отдаёт только доменные tool-имена и summary, не raw provider tool_use.id, [ADR-008](../../adr/ADR-008-provider-tool-use-id.md)).
