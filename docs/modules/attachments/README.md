# Module: Attachments (мультимодальный ввод)

- Статус: **Отложен ([TD-015](../../100-known-tech-debt.md)).** MVP мультимодального ввода реализован **inline base64** в `/v1/chat/run` без этого модуля ([ADR-020](../../adr/ADR-020-inline-base64-attachments-mvp.md), реализация — chat-orchestrator). Этот модуль (двухшаговый upload + таблица `attachments`) — **зафиксированный будущий путь**, не реализуется на MVP.
- Ответственность (отложенная): загрузка и хранение вложений (images/documents) для переиспользования между сообщениями и файлов-контекста workspace. Двухшаговая модель ([ADR-014](../../adr/ADR-014-multimodal-attachments.md)): upload → ссылка.

> **⚠️ MVP не использует этот модуль.** Поддержка фото/PDF/текста в чате на MVP — через `attachments[]` inline base64 в `POST /v1/chat/run` ([ADR-020](../../adr/ADR-020-inline-base64-attachments-mvp.md), контракт — [chat-orchestrator/02-api-contracts.md](../chat-orchestrator/02-api-contracts.md#post-v1chatrun)). Документы ниже описывают отложенную двухшаговую модель ([TD-015](../../100-known-tech-debt.md)) — реализовать при появлении требований reuse/больших файлов/object-storage.

## Документы
- [00-overview.md](00-overview.md)
- [01-context.md](01-context.md)
- [02-api-contracts.md](02-api-contracts.md)
- [03-architecture.md](03-architecture.md)
- [05-security.md](05-security.md)
- [06-rbac.md](06-rbac.md)
- [07-implementation-phases.md](07-implementation-phases.md)
- [09-testing.md](09-testing.md)

> Data model — `attachments` (таблица 16, **отложена: на MVP миграция `0004` её НЕ создаёт** — двухшаговый transport [ADR-014](../../adr/ADR-014-multimodal-attachments.md) Superseded → [TD-015](../../100-known-tech-debt.md); chat-вложения MVP — inline base64 [ADR-020](../../adr/ADR-020-inline-base64-attachments-mvp.md)). DDL — зафиксированный будущий путь ([03-data-model.md §16](../../03-data-model.md)). Общее хранилище байтов для вложений сообщений и `workspace_files` ([ADR-013](../../adr/ADR-013-workspace-projects-vs-website-builder.md)) — только в двухшаговой модели.

## DoD
- `POST /v1/attachments` (multipart) — загрузка, валидация media_type/размера, извлечение текста для PDF/text, возврат `attachmentId`.
- `GET /v1/attachments/{id}` (метаданные), `DELETE /v1/attachments/{id}`.
- `/chat/run` `attachments[]` резолвится в Anthropic content-блоки (image → vision; document → document/extracted_text) ([ADR-014](../../adr/ADR-014-multimodal-attachments.md)).
- Лимиты/allowlist, изоляция владельца. Биллинг — обычный chat-шаг (ADR-006 без изменений).

## Changelog
- 2026-06-02: bootstrap модуля (architect, Figma-gap). [ADR-014](../../adr/ADR-014-multimodal-attachments.md). Таблица `attachments`. orphan-retention → [TD-010](../../100-known-tech-debt.md); хранение в БД → [TD-009](../../100-known-tech-debt.md). См. [figma-gap-analysis.md](../../figma-gap-analysis.md).
- 2026-06-03: модуль **отложен** ([TD-015](../../100-known-tech-debt.md)). MVP мультимодального ввода — inline base64 в `/v1/chat/run` ([ADR-020](../../adr/ADR-020-inline-base64-attachments-mvp.md)), без этого модуля. Транспорт ADR-014 → Superseded; модуль сохранён как будущий путь (reuse/большие файлы/object-storage).
