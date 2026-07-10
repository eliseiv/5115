# Profile — Implementation Phases

Спринт 1. Зависит от миграции `0004` (`users.display_name`).

1. **Phase 1 — миграция:** `ALTER TABLE users ADD COLUMN display_name TEXT` (часть `0004`).
2. **Phase 2 — accountId:** чистая функция `account_id(user_id)` + unit-тесты детерминизма/формата.
3. **Phase 3 — endpoints:** `GET /v1/profile`, `PATCH /v1/profile`.
