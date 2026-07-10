# ADR-030 — `toolCallId` в элементе `serverTools[]` ответа `/chat/run`

- **Статус:** Accepted
- **Дата:** 2026-06-15
- **Связано:** [ADR-028](ADR-028-projectid-in-chat-list-and-server-tools-in-chat-response.md) (вводит `serverTools[]` и `ServerToolExecutionSchema`), [ADR-024](ADR-024-history-payload-domain-normalization.md) / [ADR-008](ADR-008-provider-tool-use-id.md) (доменный id vs provider `toolu_...`), [ADR-025](ADR-025-parallel-tool-calls-and-max-tokens-truncation.md) (`toolCalls[].id` — симметричный доменный id client-side), [ADR-011](ADR-011-server-side-tools.md) (`site.*`), [ADR-026](ADR-026-global-server-side-tools-and-time-now.md) (`time.now`), [ADR-023](ADR-023-sync-ids-in-chat-response.md) (sync-id шага/хода), [ADR-006](ADR-006-credit-billing-and-subscription-grant.md) (биллинг)

## Context

[ADR-028 Решение 2](ADR-028-projectid-in-chat-list-and-server-tools-in-chat-response.md) ввело в `ChatResponse` массив `serverTools[]` (`ServerToolExecutionSchema`) — выполненные backend за вызов server-side инструменты (`site.*` project-scoped, `time.now` global) с шейпом `{ toolName, status, summary? }`. Это **индикатор** «что отработало», а полный результат остаётся в истории `GET /v1/chats/{id}` → `steps[].payload` соответствующего tool-шага.

Репорт iOS-разработчика: в текущем шейпе нет **идентификатора вызова инструмента**, поэтому клиент не может надёжно **сопоставить** запись из `serverTools[]` с конкретным tool-шагом в истории `/v1/chats/{id}` (например, для прогресс-UI «Claude записал файл», кликабельного к деталям шага). Сопоставление по `toolName` неоднозначно, если за ход один и тот же инструмент вызван несколько раз (несколько `site.write_file` или повторный `time.now`).

Доменный `tool_call.id` (uuid4) для каждого server-side выполнения **уже существует** на стороне backend: orchestrator минтит его (`tool_call_id = uuid.uuid4()`, `orchestrator.py:951`) и уже использует в `complete_tool_call` / `add_step` (он же кладётся в `chat_steps.payload.toolCallId` tool-шага и виден в истории после нормализации [ADR-024](ADR-024-history-payload-domain-normalization.md)). То есть id уже на руках — его просто не клали в элемент `serverTools[]`.

ADR immutable → текст [ADR-028](ADR-028-projectid-in-chat-list-and-server-tools-in-chat-response.md) не переписывается; эта аддитивная правка контракта оформляется отдельным малым ADR, как [ADR-027](ADR-027-calendar-read-contract-alignment.md)/[ADR-026](ADR-026-global-server-side-tools-and-time-now.md) расширяли соседние контракты.

## Decision

В элемент `serverTools[]` (`ServerToolExecutionSchema`) добавляется поле **`toolCallId`** — АДДИТИВНО.

```jsonc
"serverTools": [
  { "toolCallId": "f2b1…uuid4", "toolName": "time.now",        "status": "completed", "summary": "ok" },
  { "toolCallId": "9c47…uuid4", "toolName": "site.write_file", "status": "completed", "summary": "ok" }
]
```

**Шейп элемента (`ServerToolExecutionSchema`) после правки:**

| Поле | Тип | Обяз. | Описание |
|---|---|---|---|
| `toolCallId` | string (uuid) | **да** | **Доменный** `tool_call.id` (uuid4) этого server-side выполнения. Совпадает с `toolCallId` соответствующего tool-шага в `GET /v1/chats/{id}` (см. инвариант ниже). **НЕ** provider `toolu_...` ([ADR-008](ADR-008-provider-tool-use-id.md)). |
| `toolName` | string | да | Доменное имя с точкой — без изменений ([ADR-028](ADR-028-projectid-in-chat-list-and-server-tools-in-chat-response.md)). |
| `status` | `"completed" \| "errored"` | да | Итог выполнения — без изменений ([ADR-028](ADR-028-projectid-in-chat-list-and-server-tools-in-chat-response.md)). |
| `summary` | string \| null | опц. | Компактный итог (≤120) — без изменений ([ADR-028](ADR-028-projectid-in-chat-list-and-server-tools-in-chat-response.md)). |

**Формат и обязательность `toolCallId`:**
- Тип — `uuid` в виде строки (как `toolCalls[].id` / `toolCall.id` в `ChatResponse` — там тоже `str`-uuid доменного формата).
- **Обязательное** (не nullable): у **каждого** выполненного server-side инструмента всегда есть доменный `tool_call_id` (минтится до исполнения, `orchestrator.py:951`), поэтому отсутствия значения быть не может. В Pydantic-схеме — `str` без `default`.
- **Позиция** — `toolCallId` ставится **первым** полем элемента (перед `toolName`), по аналогии с `toolCalls[].id`, который тоже идёт первым. На JSON-семантику порядок не влияет; делается для читаемости Swagger и симметрии с client-side `toolCalls[]`.

**Семантика (нормативно):**
- `toolCallId` = **доменный** `tool_call.id` (uuid4), тот же, что backend записал в `chat_steps.payload.toolCallId` tool-шага этого выполнения. Это **тот же домен идентификаторов**, что у client-side `toolCalls[].id` (симметрия client/server tool-id): и client-side, и server-side вызовы используют доменный uuid4 `tool_calls.id`, а не provider `toolu_...`.
- **Инвариант корреляции с историей (нормативно):** для любого элемента `serverTools[i]` существует ровно один tool-шаг в `GET /v1/chats/{id}` → `steps[]`, у которого `payload.toolCallId == serverTools[i].toolCallId` (и `payload.toolName == serverTools[i].toolName`). Это делает `serverTools[]` детерминированно сопоставимым с историей даже при повторных вызовах одного инструмента за ход. Полный результат server-side выполнения берётся из `payload` этого шага ([ADR-024](ADR-024-history-payload-domain-normalization.md)); `serverTools[]` остаётся индикатором, не каналом доставки результата.
- **Отношение к `messageStepId`/`stepId` ([ADR-023](ADR-023-sync-ids-in-chat-response.md)):** `toolCallId` идентифицирует **вызов инструмента**, а не шаг. `stepId` ответа указывает на assistant/tool-шаг ответа, не на server-side tool-шаги (они исполнены внутри tool-loop). Сопоставление server-side выполнения с историей идёт по `toolCallId` (по `payload.toolCallId` шага), а не по `stepId`.

**Аддитивность / совместимость:**
- Поле новое; добавление поля в объект — обратносовместимо: старые клиенты его игнорируют. Существующие поля (`toolName`/`status`/`summary`), их семантика, присутствие `serverTools[]` по статусам ([ADR-028](ADR-028-projectid-in-chat-list-and-server-tools-in-chat-response.md): `[]` при policy-blocked и idempotent-replay; возможно непустой при `max_tokens`), коды, security **не меняются**.
- **Биллинг неизменен** ([ADR-006](ADR-006-credit-billing-and-subscription-grant.md)): `toolCallId` информационный, на amount не влияет. Каталог инструментов и их число (**14**, [ADR-019](ADR-019-tools-catalog-endpoint.md)/[ADR-026](ADR-026-global-server-side-tools-and-time-now.md)) **не меняются**.
- **Idempotent replay** ([ADR-028 §replay](ADR-028-projectid-in-chat-list-and-server-tools-in-chat-response.md)): при повторном `/chat/tool-result` уже закрытого хода `serverTools=[]` (by-design) — добавление `toolCallId` это не меняет (массив пуст, элементов нет).

## Backend (точные указания, не код)

Источник — `src/app/chat/orchestrator.py` + маппинг в `src/app/api_gateway/routers/chat.py` + схема `src/app/schemas/chat.py`. Id уже доступен в обоих executor'ах (`tool_call_id: uuid.UUID`), правка — только проброс в аккумулятор и схему:

1. **`ServerToolExecutionOut`** (dataclass, `orchestrator.py:127`) — добавить поле `tool_call_id: uuid.UUID` (рядом с `tool_name`/`status`/`summary`).
2. **`_execute_server_side_tool`** (`orchestrator.py`, append `~:1061`) — при создании `ServerToolExecutionOut(...)` передать `tool_call_id=tool_call_id` (параметр уже в сигнатуре, уже используется в `complete_tool_call`/`add_step`).
3. **`_execute_global_server_side_tool`** (`orchestrator.py`, append `~:1124`) — аналогично передать `tool_call_id=tool_call_id`.
4. **`ServerToolExecutionSchema`** (`schemas/chat.py:237`) — добавить **первым** полем `toolCallId: str` (обязательное, без `default`), описание: доменный uuid4 вызова, совпадает с `toolCallId` tool-шага в `GET /v1/chats/{id}`.
5. **Маппинг out→schema** (`api_gateway/routers/chat.py:284-287`) — в list-comprehension добавить `toolCallId=str(st.tool_call_id)` (привести uuid к строке, как делают `toolCalls[].id`).
6. Никаких изменений билдинга `serverTools[]`, барьера хода, биллинга, истории, миграций БД — не требуется.

> Замечание для qa (информационно, не указание писать тесты): покрыть инвариант `serverTools[i].toolCallId == steps[].payload.toolCallId` соответствующего tool-шага и присутствие/обязательность поля в `serverTools[]` при `assistant_message`/`tool_call`.

## Consequences

**Плюсы:**
- iOS детерминированно сопоставляет каждую запись `serverTools[]` с tool-шагом истории `/v1/chats/{id}` по `toolCallId`, в т.ч. при повторных вызовах одного инструмента за ход.
- Симметрия идентификаторов client/server: и `toolCalls[].id` (client-side), и `serverTools[].toolCallId` (server-side) — доменный uuid4 одного домена ([ADR-008](ADR-008-provider-tool-use-id.md)/[ADR-024](ADR-024-history-payload-domain-normalization.md)).
- Правка минимальна: id уже минтится и уже течёт в историю — добавляется лишь его проброс в проекцию ответа.

**Минусы / ограничения:**
- `serverTools[]` продолжает дублировать часть информации истории (осознанно, [ADR-028](ADR-028-projectid-in-chat-list-and-server-tools-in-chat-response.md)); `toolCallId` лишь делает дубль адресуемым.
- На idempotent-replay (`serverTools=[]`) корреляция через `serverTools[]` недоступна — клиент идёт в историю (без изменений к [ADR-028](ADR-028-projectid-in-chat-list-and-server-tools-in-chat-response.md)).

## Alternatives

- **Не добавлять id, сопоставлять по `toolName` + порядок.** Отвергнуто: неоднозначно при повторных вызовах одного инструмента за ход; порядок в `serverTools[]` и порядок tool-шагов в истории не гарантированно совпадают для произвольного клиента.
- **Использовать provider `tool_use.id` (`toolu_...`).** Отвергнуто: наружу провайдерский id не отдаётся ([ADR-008](ADR-008-provider-tool-use-id.md)); клиент видит в истории доменный uuid4 — корреляция должна идти по нему ([ADR-024](ADR-024-history-payload-domain-normalization.md)).
- **Сделать `toolCallId` опциональным/nullable.** Отвергнуто: у каждого server-side выполнения id всегда есть (минтится до исполнения), nullable вводил бы ложную неопределённость в контракт.
- **Новое имя поля (`callId`/`id`).** `toolCallId` выбран для консистентности с `ToolResultItem.toolCallId` (`/chat/tool-result`) и семантической ясности (id именно вызова инструмента); `id` было бы двусмысленно (id выполнения vs id шага).
