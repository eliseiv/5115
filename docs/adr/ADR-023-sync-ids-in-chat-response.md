# ADR-023 — Идентификаторы синхронизации в `ChatResponse` (`messageStepId` + `stepId`)

- Статус: Accepted
- Дата: 2026-06-05
- Связан с: [ADR-021](ADR-021-deterministic-step-order-and-block-normalization.md) (`chat_steps.seq`, порядок шагов), [ADR-008](ADR-008-provider-tool-use-id.md) (provider `tool_use.id` vs доменный `toolCall.id`), [ADR-005](ADR-005-idempotency-ledger.md) / [ADR-006](ADR-006-credit-billing-and-subscription-grant.md) (`message_step_id` как ключ хода/идемпотентности дебита), [ADR-004](ADR-004-blocked-http-200.md) (blocked = HTTP 200), [modules/chat-orchestrator/02-api-contracts.md](../modules/chat-orchestrator/02-api-contracts.md), [modules/chats/02-api-contracts.md](../modules/chats/02-api-contracts.md)

## Context

`ChatResponse` (`src/app/schemas/chat.py`) — ответ `POST /v1/chat/run` и `POST /v1/chat/tool-result` — содержит `status` / `sessionId` / `assistantMessage` / `toolCall` / `blockReason` / `usage`, но **не отдаёт ни один id шага/хода**.

История чата уже отдаёт идентификаторы на каждый шаг: `GET /v1/chats/{id}` → `steps[]` (`ChatStepSchema`) с `id` (UUID конкретного шага = `chat_steps.id`), `messageStepId` (UUID хода = `chat_steps.message_step_id`), `role`, `payload`, `usage`, `createdAt`, упорядоченные по `chat_steps.seq` ([ADR-021](ADR-021-deterministic-step-order-and-block-normalization.md)). `GET /v1/chats/{id}/steps` группирует steps-view по `messageStepId`.

**Проблема (от iOS-разработчика).** Клиент не может сопоставить ответ генерации (`/chat/run`, `/chat/tool-result`) с шагами серверной истории: в ответе генерации нет ключа, по которому склеить оптимистично отрисованное локальное сообщение/шаг с тем же шагом, который позже придёт в `steps[]`. Без этого: дубли при рефреше истории, невозможность дедлайт-привязки tool-loop раундов к ходу, невозможность точечного обновления шага.

Обе нужные величины **уже существуют** в orchestrator:
- `message_step_id` — генерируется на старте нового user-message-шага, персистится в `chat_steps.message_step_id` / `tool_calls.message_step_id`, **един** на весь ход (все tool-раунды, включая re-entry через `/chat/tool-result`); ключ идемпотентности дебита ([ADR-005](ADR-005-idempotency-ledger.md), [ADR-006](ADR-006-credit-billing-and-subscription-grant.md)).
- `id` шага — PK строки `chat_steps` (`ChatStep.id`), отдаётся как `ChatStepSchema.id`.

То есть фикс — **чисто контрактный**: пробросить в ответ две уже доступные величины. Это не меняет хранение, биллинг, policy, коды, пути и существующие поля.

## Decision

В `ChatResponse` добавляются **два nullable-поля** (аддитивно, обратно совместимо):

| Поле | Тип | Семантика |
|---|---|---|
| `messageStepId` | UUID \| null | Ключ **хода** (один на user-message-step, переиспользуется во всех tool-раундах хода). Дословно равен `ChatStepSchema.messageStepId` соответствующих шагов этого хода в истории. |
| `stepId` | UUID \| null | Id **конкретного** assistant/tool-шага, который представляет этот ответ. Дословно равен `ChatStepSchema.id` соответствующего шага в `steps[]`. |

### Семантика по статусам `ChatResponse`

1. **`status=assistant_message`** — оба присутствуют. `messageStepId` = ход; `stepId` = `id` финального assistant-шага (тот же шаг, что появится в истории с `role=assistant`).
2. **`status=tool_call`** — оба присутствуют. `messageStepId` = ход; `stepId` = `id` assistant-шага, **содержащего `tool_use`** (тот шаг истории, чей `payload` несёт этот `tool_use`-блок). `toolCall.id` **не меняется** и остаётся доменным `tool_calls.id` (публичный id для `/chat/tool-result`); `toolCall.id` ≠ `stepId` — это разные сущности (id tool-вызова внутри шага vs id самого шага). Provider `tool_use.id` (`toolu_...`) по-прежнему наружу не отдаётся ([ADR-008](ADR-008-provider-tool-use-id.md)).
3. **`status=blocked`** — `messageStepId` = `null`, `stepId` = `null`. **Обоснование:** блокировка срабатывает в Policy Engine **до** генерации ([ADR-002](ADR-002-access-policy-state-machine.md), [ADR-004](ADR-004-blocked-http-200.md)) — ни ход (`message_step_id` дебита/хода), ни шаг (`chat_steps`) не создаются, ссылаться не на что. Поля nullable именно ради этого случая. Согласовано с тем, что при `blocked` отсутствует и `usage`.
4. **`/chat/tool-result`** (ответ — тот же `ChatResponse`) — те же правила по статусам, плюс инвариант стабильности хода: `messageStepId` ответа `/chat/tool-result` **равен** `messageStepId`, который был выдан в исходном `/chat/run` этого же хода (берётся из `tool_calls.message_step_id` по `toolCallId`, [chat-orchestrator/02-api-contracts.md §re-entry](../modules/chat-orchestrator/02-api-contracts.md)). Это и есть смысл синка tool-loop: клиент держит один `messageStepId` на весь ход. `stepId` — id **нового** шага, который представляет этот ответ (assistant-tool_use следующего раунда при `status=tool_call`, либо финальный assistant-шаг при `status=assistant_message`). Шаг-`tool_result` (запись результата клиента) — отдельная строка истории и в `stepId` ответа не возвращается: ответ всегда указывает на **следующий шаг, порождённый Claude**, а не на только что принятый результат.

### Инвариант синка (нормативно)

`ChatResponse.messageStepId` / `ChatResponse.stepId` **дословно совпадают** с `ChatStepSchema.messageStepId` / `ChatStepSchema.id` соответствующего шага в `steps[]` ответа `GET /v1/chats/{id}`. Это контракт склейки: клиент сопоставляет локально отрисованный шаг с серверной историей по `stepId` (точный шаг) и группирует раунды хода по `messageStepId`.

### Что НЕ меняется

- Существующие поля (`status`, `sessionId`, `assistantMessage`, `toolCall`, `blockReason`, `usage`) — без изменений (имена/типы/семантика).
- `toolCall.id` остаётся доменным `tool_calls.id` (не подменяется `stepId`).
- Security, HTTP-коды, пути, request-схемы — без изменений.
- Биллинг/policy/tool-loop/идемпотентность — не затрагиваются (величины уже есть, лишь экспонируются).
- Wire-формат истории (`ChatStepSchema`) — без изменений (поля уже есть).

## Rationale

- **Минимальная аддитивная дельта.** Обе величины уже вычислены и персистированы в orchestrator; решение — проброс, а не новая механика. Нет миграции, нет нового состояния.
- **Два поля, а не одно.** `messageStepId` нужен для группировки tool-loop-раундов в один ход (стабилен через re-entry); `stepId` — для точечной склейки конкретного шага. Одного `messageStepId` мало (на ход несколько шагов), одного `stepId` мало (нет группировки раундов). Оба уже отдаются в истории — симметрия контракта run/tool-result ↔ history.
- **Nullable.** Единственный статус без шага/хода — `blocked` (блок до генерации). Делать поля обязательными → пришлось бы выдумывать фиктивные id для blocked, что нарушило бы инвариант «совпадает с историей» (в истории такого шага нет). Nullable точно отражает «шага нет».
- **Не трогаем `toolCall.id`.** `toolCall.id` — публичный ключ для `/chat/tool-result` ([ADR-008](ADR-008-provider-tool-use-id.md)); подмена его на `stepId` сломала бы tool-result-роутинг. `stepId` — ортогональная величина (id шага-носителя), вводится отдельным полем.

## Consequences

- **Положительные:** клиент детерминированно склеивает ответ генерации с историей; нет дублей при рефреше; tool-loop раунды группируются по ходу. Обратно совместимо — старые клиенты игнорируют новые поля.
- **Издержки / обязательства backend:** в `ChatResponse` добавить `message_step_id` и `step_id` (`Optional[UUID]`); orchestrator при формировании ответа подставляет `message_step_id` хода и `id` персистированного assistant/tool-шага, представляющего ответ; при `blocked` — оба `None`. Сериализация — `messageStepId` / `stepId` (camelCase, как прочие поля).
- **Тестовое требование (нормативно):** тест проверяет, что для `assistant_message` и `tool_call` ответ `/chat/run` и `/chat/tool-result` несёт `messageStepId`/`stepId`, и что `stepId` совпадает с `ChatStepSchema.id` соответствующего шага в `GET /v1/chats/{id}`, а `messageStepId` стабилен в пределах хода (run → tool-result одного хода дают равный `messageStepId`); для `blocked` оба — `null`. Нормативное покрытие — раздел **«Integration — sync ids в `ChatResponse` (ADR-023)»** в [modules/chat-orchestrator/09-testing.md](../modules/chat-orchestrator/09-testing.md).

## Alternatives

- **Не отдавать id, клиент матчит по содержимому/таймстампу** — отклонён: ненадёжно (равные `created_at` у шагов одной транзакции, [ADR-021](ADR-021-deterministic-step-order-and-block-normalization.md); текст может повторяться), даёт дубли/потери.
- **Отдавать только `stepId`** — отклонён: нет ключа группировки tool-loop раундов в ход (клиент не сведёт несколько `tool_call`/итоговый `assistant_message` в одно сообщение).
- **Отдавать только `messageStepId`** — отклонён: на ход несколько шагов, точечная склейка конкретного шага невозможна.
- **Переиспользовать `toolCall.id` как stepId** — отклонён: `toolCall.id` — id tool-вызова (для tool-result-роутинга), не id шага; присутствует только при `tool_call`; подмена сломала бы [ADR-008](ADR-008-provider-tool-use-id.md).
- **Сделать поля non-nullable с фиктивным id при blocked** — отклонён: нарушает инвариант «совпадает с историей» (для blocked шага в истории нет).
