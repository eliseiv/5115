# Chat Orchestrator — Data Model

Владеет таблицами: `chat_sessions`, `chat_steps`, `tool_calls`. Полные DDL — в [03-data-model.md](../../03-data-model.md).

## chat_sessions
- `mode` фиксируется при создании, неизменяем на протяжении сессии.
- `project_id` (**nullable** с миграции `0007`, [ADR-022](../../adr/ADR-022-optional-project-and-tool-gating.md)) — external project id website-builder; фиксируется при создании, неизменяем. `NULL` → «чистый чат» без website-builder (server-side `site.*` не предлагаются). Непустая строка → website-builder доступен. При resume значение берётся из сессии; `projectId` запроса игнорируется. **НЕ** путать с `workspace_project_id` (workspace, [ADR-013](../../adr/ADR-013-workspace-projects-vs-website-builder.md)).
- `model` (`Text`, **nullable** с миграции `0010`, [ADR-034](../../adr/ADR-034-user-model-selection.md)) — выбранная пользователем модель (provider-id из allowlist активного провайдера). Фиксируется при создании сессии, неизменяем. `NULL` → дефолтная модель инстанса (`ANTHROPIC_MODEL`/`OPENAI_MODEL`) — обратная совместимость (существующие строки и запросы без `model` остаются `NULL`). При resume значение берётся из сессии; `model` запроса игнорируется. Валидация по allowlist — при создании (неизвестная модель → `422 unsupported_model`). Orchestrator передаёт `session.model or None` в `LLMClient.create_message(..., model=...)`; `None` → клиент берёт свой дефолт. Биллинг от модели не зависит ([ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md)).
- `updated_at` обновляется на каждом шаге (используется для soft TTL, [Q-001-1](../../99-open-questions.md)).

## chat_steps
- `seq` ([ADR-021](../../adr/ADR-021-deterministic-step-order-and-block-normalization.md)) — глобальный монотонный identity (`BIGINT GENERATED ALWAYS AS IDENTITY`), присваивается при INSERT в порядке вставки. **Порядок реконструкции (`list_steps`) и поиска следующего шага (`next_step_after`) — по `seq`, НЕ по `(created_at, id)`.** `created_at` — информационный timestamp (равен для шагов одной транзакции; не порядковый ключ).
- `payload` — JSONB шага. Форма зависит от `role`:
  - **`user` / `assistant`:** content blocks Anthropic (`{"content": [...]}` — text / tool_use). **Нормализован перед персистом ([ADR-021](../../adr/ADR-021-deterministic-step-order-and-block-normalization.md)):** только wire-валидные поля Anthropic; служебные SDK-поля (`caller` из `block.model_dump()`) вырезаются и не реплеятся на wire. **Хранится в СЫРОМ wire-виде:** `tool_use.name` — underscore (anthropic-формат), `tool_use.id` — provider `toolu_...` (ADR-008) — обязательно для реплея в Claude.
  - **`role="tool"` (результат tool-шага — client-side И server-side, `orchestrator.py` `_handle_tool_result`/`_handle_tool_use`):** хранится в **КАСТОМНОЙ** доменной форме, **НЕ** как wire `tool_result`-блок в `content[]`: `{ "toolCallId": <domain UUID>, "providerToolUseId": "toolu_...", "toolName": <domain dot-имя>, "result": <...|null>, "error": <{code,message}|null> }`. `toolCallId` уже **доменный** (= `tool_calls.id`); `providerToolUseId` — внутренний raw id (ADR-008), нужен для реплея continuation (`_build_messages` строит из него wire `tool_result.tool_use_id`). При отдаче истории `GET /v1/chats/{id}` ключ `providerToolUseId` **стрипается** ([ADR-024](../../adr/ADR-024-history-payload-domain-normalization.md), `_normalize_payload` → `payload.pop("providerToolUseId")`) — provider id наружу **не утекает**. Ровно одно из `result`/`error` непусто.
- **Доменная нормализация payload при отдаче истории ([ADR-024](../../adr/ADR-024-history-payload-domain-normalization.md), на копии — хранение не мутируется) покрывает ОБА пути хранения tool-результата:**
  - **(фактический путь)** кастомная форма `role="tool"` — стрип `providerToolUseId`;
  - **(forward-compat путь)** wire `tool_result`-блок внутри `content[]` (`{"type":"tool_result","tool_use_id":"toolu_...",...}`) — `_normalize_tool_result_block` подменяет `tool_use_id` (`toolu_...`→domain UUID) по карте сессии. Оркестратор сейчас этот путь **НЕ пишет** (защита на будущее/совместимость); инвариант ADR-024 на нём всё равно держится.
  - В обоих путях после нормализации provider `toolu_...` отсутствует в ответе истории; `tool_use.name` для assistant-блоков → dot (`to_domain_tool_name`), `tool_use.id` → domain UUID. Хранение `chat_steps.payload` остаётся wire-валидным для реплея.
- **Вложения ([ADR-020](../../adr/ADR-020-inline-base64-attachments-mvp.md)):** для user-turn с `attachments[]` `payload["content"]` хранит текстовый блок сообщения **+ лёгкие текстовые плейсхолдеры вложений** (`[attachment: <mediaType> "<filename>", <size> — ...]`). **Сырой base64 вложений в `payload` НЕ хранится** (инвариант): контроль раздувания БД и токенов реплея. Полные image/document/text-блоки собираются in-memory только для первого вызова Anthropic message-шага и не персистятся.
- `usage` — `{inputTokens, outputTokens, model, cacheReadTokens, cacheWriteTokens}`. Без секретов.
- `message_step_id` — billing message-step id шага: генерируется в `/chat/run`, един на весь пользовательский message-шаг (все tool-раунды и re-entry). Передаётся в `Wallet.consume` как idempotency key debit ([ADR-005](../../adr/ADR-005-idempotency-ledger.md), [ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md)). Не путать с gateway `requestId`.
- Используется для реконструкции контекста и идемпотентного возврата следующего шага.

## Инварианты порядка и нормализации (ADR-021)
- **Порядок шагов сессии определяется монотонным `seq`, НЕ `created_at`.** Все реконструкции истории и поиск «следующего шага» используют `ORDER BY seq` в пределах `session_id`. `created_at` — информационный (transaction-time `now()`, одинаков для шагов одной транзакции).
- `chat_steps.payload` содержит только wire-валидные блоки Anthropic; служебные поля SDK (`caller`) удаляются на границе персиста (нормализация по wire-схеме блока, не точечное удаление ключа).

## tool_calls
- `id` = `toolCallId` контракта (доменный UUID, **публичный** для iOS).
- `provider_tool_use_id` — raw `tool_use.id` от Anthropic (`toolu_...`, **не** UUID), **внутренний**. Записывается при разборе `tool_use` в `/chat/run`. Используется как `tool_result.tool_use_id` при continuation, чтобы пара `tool_use`/`tool_result` в истории Anthropic совпадала по id ([ADR-008](../../adr/ADR-008-provider-tool-use-id.md)). Тип `TEXT NOT NULL`.
- Принадлежность: `session_id`.
- `message_step_id` — тот же billing message-step id, что у шага, инициировавшего tool-call. Позволяет `/chat/tool-result` восстановить `messageStepId` для финального debit, не генерируя новый.
- `status`: `pending → completed | errored` (атомарный переход, ADR-005).
- `result` — сохранённый tool-result клиента (для идемпотентности повторной отправки).
- **FK завязан на `chat_sessions` (`session_id`, `ON DELETE CASCADE`), НЕ на `chat_steps`.** Удаление строк `chat_steps` (например при усечении истории, [ADR-040](../../adr/ADR-040-edit-message-and-regenerate.md)) каскадно `tool_calls` **не** удаляет — `tool_calls` усечённых ходов надо удалять **явно** по их `message_step_id`, иначе остаются осиротевшие строки. Каскад срабатывает только при удалении самой сессии ([chats DELETE](../chats/02-api-contracts.md#delete-v1chatsid)).

## Усечение истории (редактирование сообщения, [ADR-040](../../adr/ADR-040-edit-message-and-regenerate.md))
- При `editMessageStepId` в `/chat/run` оркестратор усекает историю сессии **до** записи нового user-шага: anchor = `min(seq)` шага с `role='user'` и `message_step_id = editMessageStepId`; удаляются все `chat_steps` с `seq >= anchor` **и явно** `tool_calls` усечённых ходов (см. FK-замечание выше). Всё в транзакции запроса (общий commit хода). Без user-шага → `404 message_not_found`. Метод репозитория — `truncate_from_message_step`. Изоляция: только в проверенной на владельца сессии.

## Инварианты
- Запись в `chat_steps`/`tool_calls` только этим модулем.
- `args`/`result`/`payload`/`usage` без API-ключей и секретов.
- `payload` user-turn **не содержит сырой base64 вложений** — только текстовые плейсхолдеры ([ADR-020](../../adr/ADR-020-inline-base64-attachments-mvp.md)).
- **Tool-id двойственность ([ADR-008](../../adr/ADR-008-provider-tool-use-id.md)):** доменный `id` (UUID) ↔ `provider_tool_use_id` (`toolu_...`) связаны 1:1. Наружу — только доменный UUID; в Anthropic history — только `provider_tool_use_id`. Доменный id **никогда** не используется как `tool_use.id`/`tool_result.tool_use_id` в Anthropic-протоколе. Карта `provider_tool_use_id → id` сессии — источник подмены id при доменной нормализации истории ([ADR-024](../../adr/ADR-024-history-payload-domain-normalization.md), один запрос на сессию).
