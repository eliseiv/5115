# ADR-014 — Мультимодальный ввод: вложения (images/files) в /chat/run

- Статус: **Superseded (транспортная часть) — [ADR-020](ADR-020-inline-base64-attachments-mvp.md) (2026-06-03)**
- Дата: 2026-06-02
- Связан с: chat-orchestrator, модуль `attachments`, ADR-013 (workspace-файлы), 05-security.

> **⚠️ Superseded для MVP ([ADR-020](ADR-020-inline-base64-attachments-mvp.md), 2026-06-03).** Транспортное решение этого ADR (двухшаговый upload `POST /v1/attachments` → `attachmentId` → ссылка в `/chat/run`) для MVP **заменено** на inline base64 в теле `/chat/run`. Модуль `attachments` и таблица `attachments` на MVP **не реализуются** — двухшаговый upload/storage перенесён в [TD-015](../100-known-tech-debt.md) (вернуться при reuse/больших файлах/object-storage). Концептуальные решения (allowlist, владелец=`sub`, биллинг как обычный шаг, image→image / pdf→document) сохранены в [ADR-020](ADR-020-inline-base64-attachments-mvp.md). Документ ниже сохранён как зафиксированная альтернатива / будущий путь.

## Context
Дизайн позволяет прикреплять фото и файлы к сообщению («Tasks from Photo», «Add photos», «Add files»). Сейчас `/v1/chat/run` принимает только `message` (text) + `context` (object). Нужно передать изображения в Claude (vision) и файлы (PDF/текст) как контекст.

Anthropic Messages API поддерживает в content-блоках: `image` (base64 или URL, media_type ∈ image/jpeg|png|gif|webp) и `document` (PDF, base64/URL/text). Размер vision-вложений и число ограничены.

Ключевые развилки: (а) как вложения попадают в запрос (inline base64 vs предзагрузка + ссылка); (б) лимиты; (в) совместимость с существующим size-лимитом тела `≤512KB` (ADR/05-security) — inline base64 крупной фотографии его превысит.

## Decision
Ввести **двухшаговую модель вложений** с отдельным модулем `attachments`:

1. **Upload-шаг (отдельный endpoint):** `POST /v1/attachments` (multipart/form-data) — клиент загружает бинарный файл **до** `/chat/run`. Backend валидирует media_type/размер, сохраняет байты (БД BYTEA на старте, как site_files; миграция в object-storage — общий TD-009), для PDF/текста извлекает `extracted_text`, возвращает `attachmentId` (UUID) + метаданные.
   - Лимиты: изображение ≤ 5 MB, документ ≤ 10 MB, ≤ 10 вложений на сообщение (конфигурируемо, [Q-014-2](../99-open-questions.md)).
   - Allowlist media_type: `image/jpeg`, `image/png`, `image/gif`, `image/webp`, `application/pdf`, `text/plain` ([Q-014-1](../99-open-questions.md) — расширение типов).
   - Загрузка идёт **отдельным транспортом** (multipart), поэтому НЕ нагружает JSON-size-лимит `/chat/run` (≤512KB сохраняется для JSON-тела).

2. **Reference-шаг (в /chat/run):** в тело `/chat/run` добавляется опциональное `attachments: [{ "id": "uuid" }]` — массив ссылок на ранее загруженные вложения (≤10). Orchestrator резолвит их, проверяет владельца (`attachments.user_id == sub`), собирает Anthropic content-блоки:
   - image-вложения → `image` content-block (base64 из хранилища, media_type из записи);
   - document-вложения → либо нативный `document` content-block (PDF), либо `extracted_text` как текстовый блок (стратегия по типу).
   - Vision-запросы биллятся как обычный chat-шаг (1 кредит = 1 сообщение, ADR-006 без изменений — usage в meta фиксирует image/доп. токены для аудита).

### Жизненный цикл вложений
- Вложение принадлежит пользователю (`attachments.user_id`), создаётся upload-шагом, может быть привязано к session/message при использовании (для истории) — `attachments.session_id` (nullable, проставляется при первом использовании).
- ~~Хранилище байтов вложений — общее для `attachments` и `workspace_files` (ADR-013): одна таблица `attachments` обслуживает оба (workspace_files ссылается на `attachments.id` через `content_ref`).~~ **Отменено [ADR-036 §4](ADR-036-workspaces-implementation.md):** `workspace_files` хранит байты в собственном BYTEA-столбце `content`, не ссылается на `attachments`. Общего хранилища нет.
- Retention: неиспользованные (не привязанные к session) вложения старше `ATTACHMENT_ORPHAN_TTL` (дефолт 24h) подлежат очистке — на старте без фонового джоба, помечено как [TD-010](../100-known-tech-debt.md).

## Consequences
- Новый модуль `attachments` + таблица `attachments`; контракт `POST /v1/attachments`.
- `/chat/run` дополняется опциональным `attachments[]` (обратно совместимо — отсутствие = текущее поведение).
- Vision передаётся Claude корректно без нарушения JSON-size-лимита (бинарь идёт multipart-загрузкой).
- Хранилище байтов в БД на старте → общий с website-builder TD-009 (миграция в object-storage).
- Извлечение текста из PDF требует библиотеки — фиксируется в 02-tech-stack.

## Alternatives
- **Inline base64 прямо в `/chat/run`.** Отвергнуто: ломает size-лимит `≤512KB`, раздувает JSON, неудобно для повторного использования вложения в нескольких сообщениях.
- **Полноценный object-storage с самого начала.** Отвергнуто: преждевременно; переиспользуем подход site_files (БД BYTEA) + общий TD-009.
- **Anthropic Files API (предзагрузка на стороне Anthropic).** Рассмотрено как опция оптимизации; на старте — собственное хранилище + base64/text в запросе (контроль данных, единый retention). Переход — при необходимости, не блокер.
