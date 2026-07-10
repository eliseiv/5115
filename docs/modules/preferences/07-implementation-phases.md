# Preferences — Implementation Phases

Спринт 1. Зависит от миграции `0004` (таблица `user_preferences`, enum `assistant_mode`).

1. **Phase 1 — миграция:** создать enum `assistant_mode` (если ещё не создан в `chats`-phase — единый CREATE TYPE), таблицу `user_preferences`.
2. **Phase 2 — endpoints:** `GET`/`PATCH /v1/preferences` (lazy/upsert).
3. **Phase 3 — интеграция:** orchestrator читает `default_assistant_mode` как fallback `assistantMode` ([ADR-012](../../adr/ADR-012-assistant-mode-vs-billing-mode.md)).
