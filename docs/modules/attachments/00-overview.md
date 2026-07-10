# Attachments — Overview

## Назначение
Поддержка прикрепления фото/файлов к сообщению (дизайн: «Tasks from Photo», «Add photos», «Add files») и файлов-контекста workspace. Передача изображений Claude (vision) и документов (PDF/текст) как контекста.

## Scope ([ADR-014](../../adr/ADR-014-multimodal-attachments.md))
- `POST /v1/attachments` (multipart/form-data) — загрузка бинаря **до** `/chat/run`; валидация, извлечение `extracted_text` (PDF/text), возврат `attachmentId` + метаданные.
- `GET /v1/attachments/{id}` — метаданные (без отдачи сырого бинаря наружу как файла; бинарь используется backend'ом для Anthropic).
- `DELETE /v1/attachments/{id}` — удалить вложение владельца.
- Резолв `attachments[]` в `/chat/run` (chat-orchestrator) → Anthropic content-блоки.

## Out of scope
- Object-storage (на старте БД BYTEA → [TD-009](../../100-known-tech-debt.md)).
- Фоновая очистка orphan-вложений ([TD-010](../../100-known-tech-debt.md)).
- Транскодирование/ресайз изображений (передаются как есть в пределах лимита).
- Anthropic Files API (рассмотрено как опция, не на старте — [ADR-014](../../adr/ADR-014-multimodal-attachments.md)).

## Бизнес-правила
- BR-AT-1: вложение принадлежит пользователю (`attachments.user_id == sub`); резолв чужого в `/chat/run` → `403`/`404`.
- BR-AT-2: лимиты — image ≤ 5 MB, document ≤ 10 MB, ≤ 10 вложений/сообщение ([Q-014-2](../../99-open-questions.md)).
- BR-AT-3: media_type — строго из allowlist ([Q-014-1](../../99-open-questions.md)); вне allowlist → `422`.
- BR-AT-4: для document (PDF/text) backend извлекает `extracted_text` при загрузке (подаётся Claude как текстовый контекст); для image — base64 в `image` content-block.
- BR-AT-5: биллинг — обычный chat-шаг (1 кредит = 1 сообщение, [ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md)); отдельной тарификации вложений нет на старте.
