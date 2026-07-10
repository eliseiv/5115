# Attachments — API Contracts

JWT, владелец = `sub`.

> **Статус транспорта (важно).** Активный MVP-транспорт вложений — **inline base64** в `POST /v1/chat/run` ([ADR-020](../../adr/ADR-020-inline-base64-attachments-mvp.md)), а не двухшаговый upload ниже. Описанный в этом файле `POST /v1/attachments` (multipart) — спроектированный, но **отложенный** путь ([ADR-014](../../adr/ADR-014-multimodal-attachments.md) → Superseded для транспорта; [TD-015](../../100-known-tech-debt.md)). Канонический контракт inline-вложений — [chat-orchestrator/02-api-contracts.md](../chat-orchestrator/02-api-contracts.md) и [API-REFERENCE.md §Вложения](../../API-REFERENCE.md).
>
> **PDF на обоих провайдерах.** Класс `document` (`application/pdf`) принимается и на anthropic (нативный `document`-блок), и на OpenAI (content-часть `file` или извлечённый `pypdf`-текст — фолбэк) — [ADR-041](../../adr/ADR-041-openai-native-pdf-attachment.md), закрывает [TD-023](../../100-known-tech-debt.md). Маппинг провайдер-aware ([ADR-033 §5](../../adr/ADR-033-llm-provider-abstraction.md)).

## POST /v1/attachments
Загрузка вложения (отдельным транспортом, **до** `/chat/run`).

### Request
- `Content-Type: multipart/form-data`.
- Поля: `file` (бинарь, обязателен), `kind` (`image | document`, опц. — иначе выводится из media_type).
- Transport-лимит тела: image ≤ 5 MB, document ≤ 10 MB ([Q-014-2](../../99-open-questions.md)). Превышение → `413`.

### Поведение
- Определить/проверить `media_type` по содержимому (magic bytes), не доверяя расширению/заголовку клиента; вне allowlist → `422` ([05-security.md](05-security.md)).
- Для `document` (PDF/text) — извлечь `extracted_text` (усечение до лимита контекста).
- Сохранить в `attachments` (`user_id=sub`, `session_id=NULL`).

### Response (201)
```json
{
  "id": "uuid",
  "kind": "image | document",
  "mediaType": "image/png",
  "filename": "string | null",
  "size": 12345,
  "hasExtractedText": true,
  "createdAt": "ISO8601"
}
```
- Сырой бинарь и `extracted_text` наружу не возвращаются (используются backend'ом при сборке Anthropic-запроса).

## GET /v1/attachments/{id}
Метаданные вложения владельца.
### Response (200)
Та же схема, что POST-ответ. Чужое/несуществующее → `404`.

## DELETE /v1/attachments/{id}
### Response (200)
```json
{ "deleted": true }
```
- `workspace_files` больше **не** ссылается на `attachments` ([ADR-036 §4](../../adr/ADR-036-workspaces-implementation.md): workspace-файлы хранятся собственным BYTEA); связи нет. `attachments.session_id` references — независимы.

## Использование в /chat/run (chat-orchestrator)
- В теле `/chat/run`: `attachments: [{ "id": "uuid" }]` (≤ 10). См. [chat-orchestrator/02-api-contracts.md](../chat-orchestrator/02-api-contracts.md).
- Orchestrator: проверка владельца (`attachments.user_id == sub`, иначе `403`/`404`), сборка content-блоков:
  - `kind=image` → Anthropic `image` block (base64, media_type из записи).
  - `kind=document` → Anthropic `document` block (PDF) **или** текстовый блок из `extracted_text` (по типу).
- Проставление `attachments.session_id` при первом использовании (для истории чата).
