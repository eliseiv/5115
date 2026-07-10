# ADR-028 — `projectId` в списке чатов + выполненные server-side инструменты в ответе `/chat/run`

- **Статус:** Accepted
- **Дата:** 2026-06-12
- **Связано:** [ADR-022](ADR-022-optional-project-and-tool-gating.md) (опциональный `projectId`), [ADR-011](ADR-011-server-side-tools.md) (`site.*`), [ADR-026](ADR-026-global-server-side-tools-and-time-now.md) (`time.now`), [ADR-025](ADR-025-parallel-tool-calls-and-max-tokens-truncation.md) (`toolCalls[]` + max_tokens), [ADR-024](ADR-024-history-payload-domain-normalization.md) (`assistantMessage` при `tool_call`), [ADR-023](ADR-023-sync-ids-in-chat-response.md) (sync-id), [ADR-006](ADR-006-credit-billing-and-subscription-grant.md) (биллинг)

## Context

Два независимых репорта iOS-разработчика по полноте контракта (оба — **аддитивные** правки, не breaking):

1. **`projectId` не виден в списке чатов.** При создании сессии можно передать свободную строку `projectId` ([ADR-022](ADR-022-optional-project-and-tool-gating.md)) — она сохраняется в `chat_sessions.project_id` и используется как ключ website-builder. В ответе `GET /v1/chats` этого поля **нет**: есть только `workspaceProjectId` (UUID-заготовка под будущий модуль `workspaces`, Спринт 2, [ADR-013](ADR-013-workspace-projects-vs-website-builder.md)), который захардкожен в `null`. iOS не может в списке чатов отличить «чат с проектом» (website-builder) от «чистого чата» и не видит, к какому проекту привязан чат, без запроса истории.

2. **Выполненные server-side инструменты не видны в ответе `/chat/run`.** По дизайну ([ADR-011](ADR-011-server-side-tools.md), [ADR-026](ADR-026-global-server-side-tools-and-time-now.md)) server-side инструменты (`site.*` project-scoped, `time.now` global) backend исполняет сам внутри tool-loop **без** round-trip к iOS; в `toolCalls[]` ответа попадают только **client-side** вызовы (их исполняет iOS). Наружу через `/chat/run` идёт только финальный `assistantMessage` (или client-side `toolCalls[]`). Какие server-side инструменты отработали за ход, видно лишь постфактум в `GET /v1/chats/{id}` и `GET /v1/chats/{id}/steps`. Для основного флоу через `/chat/run` разработчик не видит факта и итога server-side выполнений (например, что Claude вызвал `time.now` или записал файл сайта) без отдельного запроса истории.

Оба пробела — про **наблюдаемость контракта для iOS**. Решаются одним ADR (оба выявлены одним прогоном репортов, общая тема, обе правки аддитивные и не пересекаются по семантике); внутри ADR — два независимых решения с явной пометкой аддитивности у каждого.

## Decision

### Решение 1 — `projectId` в элементе списка чатов (`GET /v1/chats`) — АДДИТИВНО

В элемент списка чатов (`ChatListItemSchema`) добавляется поле:

```jsonc
"projectId": "string | null"   // = chat_sessions.project_id (свободная строка, ADR-022)
```

- **Семантика** — та же, что у `projectId` в `/v1/chat/run` ([ADR-022](ADR-022-optional-project-and-tool-gating.md)): свободная строка-идентификатор website-builder-проекта, заданная при создании сессии. Формат и значение **идентичны** значению, которое клиент передал в `/chat/run`.
- **`null` = «чистый чат»** — сессия создана без `projectId` (website-builder для неё не активирован, `site.*` Claude не предлагались). Это нормальный основной режим сервиса ([ADR-022](ADR-022-optional-project-and-tool-gating.md)).
- **`workspaceProjectId` НЕ трогается** — остаётся как есть (UUID-заготовка под Спринт-2 модуль `workspaces`, всегда `null` до создания колонки `chat_sessions.workspace_project_id`, [ADR-013](ADR-013-workspace-projects-vs-website-builder.md)). Поля **независимы и не взаимозаменяемы**: `projectId` — свободная строка website-builder (есть сейчас), `workspaceProjectId` — UUID рабочего пространства (Спринт 2). Оба присутствуют в ответе одновременно.
- **Позиция поля** — `projectId` ставится в `ChatListItemSchema` непосредственно **перед** `workspaceProjectId` (сгруппировать два «проектных» поля; стабильный порядок для читаемости Swagger; на JSON-семантику порядок не влияет).
- **Аддитивность** — поле новое и nullable; старые клиенты его игнорируют. Существующие поля (`id`/`title`/`preview`/`assistantMode`/`isPinned`/`workspaceProjectId`/`updatedAt`), сортировка, пагинация, коды и security **не меняются**. Не breaking.

### Решение 2 — выполненные server-side инструменты в ответе `/chat/run` (и `/chat/tool-result`) — АДДИТИВНО

В `ChatResponse` (ответ `/v1/chat/run` и `/v1/chat/tool-result`) добавляется массив **выполненных за этот вызов** server-side инструментов:

```jsonc
"serverTools": [
  { "toolName": "time.now",        "status": "completed", "summary": "ok" },
  { "toolName": "site.write_file", "status": "completed", "summary": "index.html" }
]
```

**Имя поля — `serverTools`** (выбрано вместо `executedServerTools`). Обоснование: короче (важно для частого ответа), согласуется по стилю с существующим `toolCalls` (без глагольного префикса); семантику «выполненные/завершённые» несёт `status` каждого элемента и описание поля. Альтернатива `executedServerTools` отвергнута как избыточно длинная при той же информативности.

**Шейп элемента (`ServerToolExecutionSchema`):**

| Поле | Тип | Описание |
|---|---|---|
| `toolName` | string (обяз.) | Доменное имя с точкой (`time.now`, `site.write_file`, `site.preview`, …) — совпадает с `/v1/tools` `name` и `GET /v1/chats/{id}/steps` `toolName`. |
| `status` | `"completed" \| "errored"` (обяз.) | Итог выполнения. `completed` — успех; `errored` — инструмент вернул ошибку (tool-result error), при этом ход **не падает** (как в steps-view). Совпадает со статусом `tool_calls` (`completed`/`errored`), который backend уже выставляет в `_execute_server_side_tool` / `_execute_global_server_side_tool`. |
| `summary` | string \| null (опц.) | **Компактный** человекочитаемый итог. Жёсткий лимит длины — `_SUMMARY_MAX_CHARS = 120` (тот же, что в steps-view). **НЕ raw result.** |

**Содержимое `summary` (нормативно по безопасности/размеру — финальная MVP-реализация, [Q-028-1](../99-open-questions.md) Closed):**
- **Запрещено** класть полный raw-результат инструмента. `site.*` может вернуть большой объём, пути, URL, имена файлов превью со signed-token и т.п. — это **не** отдаётся в `serverTools[].summary`.
- `summary` несёт **строго** один из двух вариантов (единый компактный формат, реализовано в `_server_tool_summary`): для `completed` — **только** литерал `"ok"`; для `errored` — **только** короткий машинный `error_code` (например `"invalid_timezone"`). **Никогда** не читаются и не попадают в ответ: raw result, `error_message`/детали ошибки, стектрейсы, пути, URL, signed-token. Перед укладкой `summary` обрезается до `_SUMMARY_MAX_CHARS` (`= 120`; коды и так короче — обрезка защитная).
- Полный результат server-side инструмента остаётся доступен **только** в истории (`GET /v1/chats/{id}` → `steps[].payload` tool-шага, после доменной нормализации [ADR-024](ADR-024-history-payload-domain-normalization.md)) и в steps-view. `serverTools[]` — это компактный индикатор «что отработало», **не** канал доставки результата.
- Доменное per-tool обогащение `summary` (например имя файла для `site.write_file`) — **возможное будущее усиление**, на MVP **не** реализуется ([Q-028-1](../99-open-questions.md) Closed дефолтом); при появлении потребности вводится отдельным уточнением без слома типа/лимита/`status` поля.

**Семантика «за один вызов `/chat/run`» (не за всю сессию):** `serverTools[]` перечисляет server-side инструменты, выполненные backend в tool-loop **этого** обращения (`/chat/run` или один `/chat/tool-result`-continuation), в порядке выполнения. Дубликаты с историей `/chats` — допустимы и ожидаемы (это удобство флоу, **не** замена истории). За многораундовый ход (несколько server-side раундов до финала) массив накапливает **все** server-side выполнения этого вызова.

**Присутствие поля по статусам:**

| `status` ответа | `serverTools[]` |
|---|---|
| `assistant_message` (финал) | присутствует; перечисляет все server-side выполнения этого вызова (может быть пустым `[]`, если их не было) |
| `tool_call` (модель запросила client-side) | присутствует; перечисляет server-side, выполненные в этом вызове **до** момента, когда ход уперся в client-side tool_call (может быть `[]`) |
| `blocked` + `policy` (`blockReason ≠ max_tokens`) | **пустой `[]`** — policy-block срабатывает **до** генерации ([ADR-002](ADR-002-access-policy-state-machine.md)), tool-loop не запускался, server-side не выполнялись |
| `blocked` + `max_tokens` ([ADR-025](ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)) | присутствует; **может быть НЕ пустым** — server-side раунды могли отработать **до** того, как финальный виток обрезался по `max_tokens`. Перечисляет уже выполненные server-side за этот вызов |
| `assistant_message` через **idempotent replay** continuation (`_render_saved_step`, повтор `/chat/tool-result` уже закрытого хода) | **пустой `[]`** — by-design (см. ниже) |

Поле присутствует **всегда** (в т.ч. как пустой массив `[]`) при `assistant_message`/`tool_call`/`blocked` — клиент не различает «нет поля» и «пустой массив». Семантика однородна: «что server-side отработало за этот вызов».

**Idempotent replay → `serverTools=[]` (by-design, нормативно).** Повторный `/chat/tool-result` для **уже закрытого** хода ([ADR-005](ADR-005-idempotency-ledger.md)/[ADR-025](ADR-025-parallel-tool-calls-and-max-tokens-truncation.md): continuation выполняется один раз на закрытие барьера) возвращает **сохранённый** финальный шаг через `_render_saved_step`, который отдаёт `serverTools=[]` — server-side выполнения при повторном рендере сохранённого шага **НЕ** реконструируются. Это осознанное by-design-поведение: реплей отдаёт финальный результат хода, а не воспроизводит tool-loop; `serverTools[]` — индикатор «что отработало **в этом** вызове», а на чистом replay backend ничего не исполнял. Полный набор server-side выполнений хода остаётся доступен в истории `GET /v1/chats/{id}` (steps tool-шагов).

**Биллинг и совместимость:**
- Биллинг **неизменен** ([ADR-006](ADR-006-credit-billing-and-subscription-grant.md)): server-side раунды не порождают списаний; 1 кредит = 1 финальный `assistant_message`-шаг. `serverTools[]` — информационное поле, на amount не влияет.
- Поле аддитивное и nullable-по-смыслу (всегда массив, возможно пустой); старые клиенты игнорируют. Существующие поля `ChatResponse` (`status`/`sessionId`/`messageStepId`/`stepId`/`assistantMessage`/`toolCall`/`toolCalls`/`blockReason`/`usage`), их семантика, коды и security **не меняются**. Не breaking.
- Каталог инструментов и их число (**14**, [ADR-019](ADR-019-tools-catalog-endpoint.md)/[ADR-026](ADR-026-global-server-side-tools-and-time-now.md)) **не меняются** — ADR добавляет поле в ответ, а не инструмент.

**Согласование со `StepsViewResponse`:** идея компактного `summary` (≤ `_SUMMARY_MAX_CHARS`) переиспользуется из steps-view (`StepsViewStepSchema.summary`), но `serverTools[]` — **отдельное** поле со своей семантикой: только **server-side** выполнения (steps-view покрывает все шаги — reasoning/tool_call/tool_result/assistant_message client + server), и `status` (`completed`/`errored`) вместо `kind`. Не дублирование: steps-view — диагностический срез истории по `messageStepId` (отдельный GET), `serverTools[]` — inline-индикатор в самом ответе генерации.

## Consequences

**Плюсы:**
- iOS видит привязку чата к website-builder-проекту прямо в списке (`projectId`) и отличает «чистые чаты» от проектных без запроса истории.
- iOS видит факт и компактный итог server-side выполнений прямо в ответе `/chat/run` (прогресс-UI «Claude уточнил время / записал файл») без дополнительного запроса истории/steps-view.
- Обе правки аддитивны — старые клиенты не ломаются; миграции БД/контракта не требуются (поля читаются из уже существующих `chat_sessions.project_id` и статусов `tool_calls`).

**Минусы / ограничения:**
- `serverTools[]` дублирует часть информации истории (`/chats`) — осознанно (удобство флоу), не замена истории; полный результат — только в истории.
- `summary` намеренно беден (компактный, без raw) — для полного результата клиент идёт в `GET /v1/chats/{id}`. Это плата за безопасность/размер (не утекают пути/URL/токены `site.*`).
- Формат `summary` зафиксирован финально ([Q-028-1](../99-open-questions.md) **Closed** дефолтом): единый компактный формат — `"ok"` при `completed`, `error_code` при `errored`, без raw/`error_message`/путей/URL/токенов (реализовано в `_server_tool_summary`). Доменное per-tool обогащение — отдельная будущая задача по реальной потребности, не MVP; контракт поля при этом не меняется.

## Alternatives

- **Два отдельных ADR.** Отвергнуто: обе правки — мелкие аддитивные, выявлены одним прогоном репортов, общая тема (полнота контракта для iOS), не пересекаются. Один ADR с двумя явно разведёнными решениями — компактнее и трассируемее.
- **Имя `executedServerTools`.** Отвергнуто в пользу `serverTools` (короче, стиль `toolCalls`); «выполненные» несёт `status` + описание.
- **Класть raw-результат server-side в ответ.** Отвергнуто по безопасности/размеру: `site.*` может вернуть большой payload с путями/URL/signed-token. Только `status` + компактный `summary`; полный результат — в истории.
- **Отдавать `serverTools[]` только при `assistant_message`.** Отвергнуто: server-side могут отработать и в ходе, завершающемся `tool_call` (сначала server-side, потом модель попросила client-side) и даже при `max_tokens` (server-side раунды до обрыва). Поле присутствует во всех не-policy статусах, чтобы клиент видел server-side выполнения независимо от того, чем закончился ход.
- **Использовать `workspaceProjectId` для `project_id`.** Отвергнуто: это разные сущности ([ADR-013](ADR-013-workspace-projects-vs-website-builder.md)) — UUID workspace (Спринт 2) vs свободная строка website-builder (сейчас). Подмена сломала бы типы и семантику. Добавляется отдельное поле `projectId`.
