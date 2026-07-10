# Notifications — Implementation Phases

Спринт 3 (или отдельный поздний спринт — низкий приоритет, частично [TD-011](../../100-known-tech-debt.md)).

1. **Phase 1 — миграция:** таблица `device_push_tokens` + индексы. (`notifications_enabled` — в `user_preferences`, миграция `0004`, модуль preferences.)
2. **Phase 2 — token CRUD:** `POST`/`DELETE /v1/notifications/device-token`.
3. **Phase 3 (TD-011, отдельный проход) — отправка:** APNs-клиент, триггеры, уважение `notifications_enabled`. **Не в этом проходе.**

> Если объём Спринта 3 велик — Phase 1–2 можно вынести в отдельный мини-спринт; ядро (Спринт 1) от этого не зависит.
