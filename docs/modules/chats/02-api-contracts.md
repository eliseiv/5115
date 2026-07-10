# Chats — API Contracts

Все эндпоинты — JWT, владелец = `sub`. Чужой/несуществующий чат → `404`.

## GET /v1/chats
Список чатов пользователя.

### Query
- `q` (опц.) — поиск: ILIKE по `title` и по тексту первого user-сообщения.
- `cursor` (опц.) — пагинация (opaque, по `updated_at`+`id`).
- `limit` (опц., дефолт 30, max 100).
- `workspaceProjectId` (опц., uuid, [ADR-036](../../adr/ADR-036-workspaces-implementation.md)) — **фильтр «чаты проекта»**: возвращает только чаты, привязанные к указанному workspace (`chat_sessions.workspace_project_id = :id`). Чужой/несуществующий workspace → пустой список (изоляция по `sub`, не `404` для фильтра-параметра). Без параметра — все чаты пользователя (поведение неизменно).

### Response (200)
```json
{
  "items": [
    {
      "id": "uuid",
      "title": "string | null",
      "preview": "string (срез последнего сообщения)",
      "assistantMode": "chat | code",
      "isPinned": false,
      "projectId": "string | null",
      "workspaceProjectId": "uuid | null",
      "updatedAt": "ISO8601"
    }
  ],
  "nextCursor": "string | null"
}
```
- Сортировка: `is_pinned DESC, updated_at DESC` (BR-CH-3).
- **`preview` без conversation-settings блока ([ADR-042](../../adr/ADR-042-hide-context-block-from-user-facing-history.md)):** превью user-сообщения **не** содержит ведущий служебный блок `[Conversation settings for this message: …]` ([ADR-037](../../adr/ADR-037-chatrunrequest-context-allowlist-injection.md) §4). Блок персистится внутри текста user-шага (для replay), но **срезается при отдаче**: превью формируется в `ChatsRepository._preview` / `_text_from_payload` (`src/app/chats/repository.py`), где к тексту первого text-блока user-шага применяется единый helper `strip_context_block` **строго ДО** `_truncate` (collapse в `_truncate` схлопывает `\n\n` и сломал бы якорь среза). Превью показывает только текст пользователя; если сообщение было image-only/file-only с `context` (текст = только блок) — превью этого шага пусто́.
- **`projectId` (свободная строка, [ADR-028](../../adr/ADR-028-projectid-in-chat-list-and-server-tools-in-chat-response.md), аддитивно):** `= chat_sessions.project_id` — тот же свободный строковый идентификатор website-builder-проекта, что клиент передал в `POST /v1/chat/run` при создании сессии ([ADR-022](../../adr/ADR-022-optional-project-and-tool-gating.md)). Формат и семантика **идентичны** `projectId` из `/chat/run`. **`null` = «чистый чат»** — сессия создана без `projectId` (website-builder не активирован, `site.*` Claude не предлагались); это основной режим сервиса. Поле позволяет iOS в списке отличить проектные чаты от чистых без запроса истории.
- **`projectId` ≠ `workspaceProjectId`** — независимые, не взаимозаменяемые поля ([ADR-013](../../adr/ADR-013-workspace-projects-vs-website-builder.md)): `projectId` — свободная строка website-builder, `workspaceProjectId` — UUID рабочего пространства. Оба присутствуют в ответе одновременно.
- **`workspaceProjectId` (Поставка 3, [ADR-036](../../adr/ADR-036-workspaces-implementation.md)) — реальное значение** из `chat_sessions.workspace_project_id` (более не заглушка-null). `null` = чат без workspace. До миграции `0011` (пока колонки нет) сервис отдаёт `null`; после — фактическую привязку.
- N+1 на `preview` (отдельный запрос на каждый чат страницы) — осознанный tech-debt [`TD-012`](../../100-known-tech-debt.md), приемлемо для текущего per-user масштаба.

## GET /v1/chats/{id}
История шагов чата.

### Response (200)
```json
{
  "id": "uuid",
  "title": "string | null",
  "assistantMode": "chat | code",
  "mode": "credits | byok",
  "steps": [
    {
      "id": "uuid",
      "messageStepId": "uuid",
      "role": "user | assistant | tool",
      "payload": { },
      "usage": { "inputTokens": 0, "outputTokens": 0, "model": "string" },
      "createdAt": "ISO8601"
    }
  ]
}
```
- `steps` — упорядочены по `chat_steps.seq` (монотонный порядок вставки, [ADR-021](../../adr/ADR-021-deterministic-step-order-and-block-normalization.md)), **НЕ** по `created_at` (равен для шагов одной транзакции). `createdAt` отдаётся как информационный timestamp каждого шага.
- `payload` — payload шага. **Отдаётся в ДОМЕННОМ виде (нормализация при отдаче, [ADR-024](../../adr/ADR-024-history-payload-domain-normalization.md)), а НЕ в сыром виде хранилища.** Нормализация применяется на границе сериализации ответа (хранение `chat_steps.payload` и реплей в Claude не меняются). Форма зависит от `role`:
  - **`role="tool"` (результат tool-шага):** хранится в кастомной доменной форме `{toolCallId (domain UUID), providerToolUseId, toolName (dot), result|error}` — **НЕ** wire `tool_result`-блок в `content[]` (см. [chat-orchestrator/04-data-model.md](../chat-orchestrator/04-data-model.md)). При отдаче `providerToolUseId` **стрипается** (внутренний raw `toolu_...`, ADR-008 — наружу не утекает); `toolCallId` уже доменный (= `tool_calls.id`, совпадает с `/chat/run` `toolCall.id`).
  - **`role="assistant"` content-блоки (`type=text` / `type=tool_use`):**
    - **`tool_use.name`:** underscore → dot (`calendar_create_events` → `calendar.create_events`) через `to_domain_tool_name` — совпадает с `/v1/tools` `name`, `/chat/run` `toolCall.name`, `/v1/chats/{id}/steps` `toolName`.
    - **`tool_use.id`:** провайдерский `toolu_...` → доменный `tool_calls.id` (UUID) по карте `provider_tool_use_id → id` сессии (один запрос на сессию, без N+1). Совпадает с `/chat/run` `toolCall.id`. Provider `toolu_...` наружу в истории **не утекает**.
  - **Wire `tool_result`-блок в `content[]`** — альтернативный путь, который нормализация ADR-024 тоже покрывает (`_normalize_tool_result_block`: `tool_use_id` `toolu_...`→domain UUID), но оркестратор его сейчас **не пишет** (результат tool-шага идёт кастомной формой выше; путь оставлен как forward-compat-защита). На обоих путях provider `toolu_...` наружу не утекает.
  - **Текстовые блоки (`type=text`):** байт-в-байт как в хранилище, **за единственным исключением** — ведущий conversation-settings блок ADR-037 срезается у user-шагов ([ADR-042](../../adr/ADR-042-hide-context-block-from-user-facing-history.md), см. ниже). `tool_use.input` — не меняется.
  - **User-шаг без conversation-settings блока ([ADR-042](../../adr/ADR-042-hide-context-block-from-user-facing-history.md)):** если сообщение было отправлено с `context` ([ADR-037](../../adr/ADR-037-chatrunrequest-context-allowlist-injection.md)), backend персистит в `chat_steps.payload` текст с **лидирующим** служебным блоком `[Conversation settings for this message: …]\n\n` (для корректного replay — ADR-037 §4). В истории этот блок **срезается при отдаче** (на той же копии, что и нормализация ADR-024): у шага `role="user"` из первого `text`-блока убирается ведущий якорь `^\[Conversation settings for this message: [^\]]*\]\n\n` (единый helper `strip_context_block`, источник истины формата). Остаётся **только текст пользователя**. assistant/tool-шаги не трогаются; нет блока → no-op. **Хранение `chat_steps.payload` и реплей модели (`_build_messages`) НЕ меняются** — модель по-прежнему получает блок. **Edge image-only/file-only с `context`** ([ADR-039](../../adr/ADR-039-optional-message-with-attachments.md)): текст user-шага был = только блок (без хвоста `\n\n`); после среза текст **пустой** — в истории показывается пустой текст + attachment-плейсхолдеры (исходное сообщение было без текста, корректно).
  - **Полнота шага (нестыковка 3, [ADR-024](../../adr/ADR-024-history-payload-domain-normalization.md)):** один assistant-шаг МОЖЕТ содержать `payload.content = [text, tool_use]` (или несколько `tool_use` при parallel tool use) вместе. История отдаёт **полный, упорядоченный** массив блоков шага — это канонический источник полного хода (в отличие от дискриминированного `ChatResponse`, который отдаёт одно состояние раунда). Клиент читает полный ход из `steps[].payload.content[]`.
- **Инвариант синка имени/id (нормативно, [ADR-024](../../adr/ADR-024-history-payload-domain-normalization.md)):** в любом `tool_use`/`tool_result`-блоке истории `name` (dot) и `id`/`tool_use_id` (domain UUID) **дословно совпадают** с `/chat/run` `toolCall.name`/`toolCall.id` того же вызова и с `/v1/tools` `name`.
- **Синк id шага/хода ([ADR-023](../../adr/ADR-023-sync-ids-in-chat-response.md)):** `steps[].id` (= `chat_steps.id`) и `steps[].messageStepId` (= `chat_steps.message_step_id`) — те же значения, что отдаёт `ChatResponse.stepId` / `ChatResponse.messageStepId` ([chat-orchestrator/02-api-contracts.md](../chat-orchestrator/02-api-contracts.md#response-200)) для соответствующего шага/хода. Клиент склеивает оптимистично отрисованный шаг с серверной историей по `id` (точный шаг) и группирует tool-loop-раунды хода по `messageStepId`.
- **Как ссылаться на сообщение для редактирования ([ADR-040](../../adr/ADR-040-edit-message-and-regenerate.md)):** чтобы отредактировать ранее отправленное сообщение, клиент передаёт его **`messageStepId`** (а **не** `stepId`) в поле `editMessageStepId` запроса `POST /v1/chat/run` ([chat-orchestrator/02-api-contracts.md §editMessageStepId](../chat-orchestrator/02-api-contracts.md#editmessagestepid-adr-040)). Значение берётся из `steps[].messageStepId` истории (хода user-сообщения) **или** из `ChatResponse.messageStepId`, полученного при отправке этого сообщения. Один `messageStepId` идентифицирует весь ход (user-шаг + ответ ассистента + tool-раунды); backend усекает историю от этого хода и генерирует заново. `stepId` для редактирования **не** используется (он адресует конкретный шаг, а редактируется весь ход целиком).

## GET /v1/chats/{id}/steps
Steps-view для UI («N steps»): агрегированные шаги последнего (или указанного) message-шага — tool-calls и assistant-reasoning.

### Query
- `messageStepId` (опц.) — конкретный message-шаг; по умолчанию — последний.

### Response (200)
```json
{
  "messageStepId": "uuid",
  "stepCount": 3,
  "steps": [
    {
      "kind": "reasoning | tool_call | tool_result | assistant_message",
      "toolName": "string | null",
      "summary": "string (краткое описание шага для UI)",
      "createdAt": "ISO8601"
    }
  ]
}
```
- Источник — `chat_steps` + `tool_calls` по `message_step_id`. Порядок шагов внутри message-шага — по `chat_steps.seq` ([ADR-021](../../adr/ADR-021-deterministic-step-order-and-block-normalization.md)), НЕ `created_at`. `toolName` — доменное имя (с точкой), как в `tool_calls.tool_name`. Никаких секретов/raw provider id наружу.
- **Parallel tool use ([ADR-025](../../adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)):** assistant-ход с несколькими `tool_use`-блоками порождает несколько строк `tool_calls` (по одной на вызов) → несколько `kind=tool_call` шагов steps-view одного `messageStepId` (по `toolName` каждого). Это согласуется с `toolCalls[]` ответа `/chat/run` — каждый элемент массива соответствует своему `tool_call`-шагу.

## PATCH /v1/chats/{id}
Переименование, закрепление и/или **перенос чата в воркспейс** ([ADR-038](../../adr/ADR-038-move-chat-to-workspace.md)).

### Request
```json
{ "title": "string (optional)", "isPinned": true, "workspaceProjectId": "uuid | null (optional)" }
```
- Хотя бы одно поле (`title` / `isPinned` / `workspaceProjectId` — присутствие любого в теле). `extra='forbid'`. `title` ≤ 200 символов.
- **`workspaceProjectId` ([ADR-038](../../adr/ADR-038-move-chat-to-workspace.md)) — управление привязкой чата к воркспейсу:**
  - **поле отсутствует** в теле → привязка **не трогается** (меняются только переданные `title`/`isPinned`);
  - **`workspaceProjectId = <uuid>`** → **перенести/сменить**: `chat_sessions.workspace_project_id = uuid`. Валидируется принадлежность целевого workspace пользователю (`sub`); чужой/несуществующий → **`404 workspace_not_found`** (изоляция, **консистентно с `POST /v1/chat/run`**, [ADR-036 §3](../../adr/ADR-036-workspaces-implementation.md));
  - **`workspaceProjectId = null`** → **убрать из воркспейса**: `chat_sessions.workspace_project_id = NULL` (чат становится обычным «чистым» чатом).
  - Различение «поле отсутствует» vs «поле = null» — по `model_fields_set` (как для `title`). **Идемпотентно:** повторный PATCH с тем же значением (uuid или null) → `200`, без ошибки.
- Чужой/несуществующий **чат** → `404` (как у всех `/v1/chats/*`).
- **Связь с session-fixed семантикой ([ADR-038 §4](../../adr/ADR-038-move-chat-to-workspace.md)):** `workspaceProjectId` в `POST /v1/chat/run` остаётся **session-fixed** (устанавливается при создании сессии, на resume игнорируется, [chat-orchestrator/02-api-contracts.md](../chat-orchestrator/02-api-contracts.md#workspaceprojectid-adr-036)). Изменять привязку у существующего чата можно **только** этим `PATCH` — единый путь записи после создания, без конкуренции с `/chat/run`.
- **Эффект переноса на генерацию ([ADR-038 §3](../../adr/ADR-038-move-chat-to-workspace.md)):** после переноса `instructions` целевого workspace начинают подмешиваться в system-prompt со **следующего** сообщения чата (orchestrator инъектирует instructions на каждом ходе сессии с workspace). **Файлы-знания ретроспективно НЕ подмешиваются** (вариант a) — они подаются только чатам, созданным в воркспейсе изначально (turn 0). Чтобы файлы участвовали с самого начала — создавайте чат уже в воркспейсе (`/chat/run` с `workspaceProjectId`). Ретроактивная подача файлов → [Q-038-1](../../99-open-questions.md).
- Биллинг неизменен — PATCH (включая перенос) не списывает кредиты ([ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md)). Миграции нет (колонка `chat_sessions.workspace_project_id` уже есть, `0011`).

### Response (200)
```json
{ "id": "uuid", "title": "string | null", "isPinned": false, "workspaceProjectId": "uuid | null", "updatedAt": "ISO8601" }
```
- `workspaceProjectId` — актуальная привязка после изменения (или `null`). Аддитивно/обратносовместимо: старые клиенты игнорируют новое поле ([ADR-038](../../adr/ADR-038-move-chat-to-workspace.md)).

## DELETE /v1/chats/{id}
Удаление чата.

### Response (200)
```json
{ "deleted": true }
```
- Каскадно удаляет `chat_steps`/`tool_calls` (FK). `attachments.session_id` → NULL. Идемпотентно: повторный DELETE уже удалённого → `404`.
