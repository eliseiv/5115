# Snippets — Implementation Phases

Спринт 2. Таблица `snippets` создаётся **отдельной будущей миграцией** (НЕ `0004` — `0004` создаёт только `user_preferences` + поля `chat_sessions`/`users`).

1. **Phase 1 — миграция:** таблица `snippets` + индексы (`ix_snippets_user_created`, `ix_snippets_user_language`) — отдельная будущая миграция Спринта 2.
2. **Phase 2 — CRUD/list:** `GET` (фильтр/поиск/пагинация), `POST`, `GET /{id}`, `PATCH`, `DELETE`.
