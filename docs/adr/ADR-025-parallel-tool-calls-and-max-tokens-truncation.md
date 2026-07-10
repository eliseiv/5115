# ADR-025 — Параллельные client-side tool-вызовы + max_tokens / обрезка ответа

- Статус: Accepted
- Дата: 2026-06-10
- Связан с: [ADR-006](ADR-006-credit-billing-and-subscription-grant.md) (1 кредит = 1 message-step — **не меняется**), [ADR-008](ADR-008-provider-tool-use-id.md) (provider `tool_use.id` vs доменный `toolCall.id`; parallel tool use по построению поддержан поблочно), [ADR-011](ADR-011-server-side-tools.md) (server-side `site.*` исполняются на бэке в tool-loop — в `toolCalls[]` не попадают), [ADR-021](ADR-021-deterministic-step-order-and-block-normalization.md) (`chat_steps.seq`, нормализация content-блоков), [ADR-023](ADR-023-sync-ids-in-chat-response.md) (`messageStepId`/`stepId` синка), [ADR-024](ADR-024-history-payload-domain-normalization.md) (доменная нормализация истории; `assistantMessage` при `tool_call`), [ADR-004](ADR-004-blocked-http-200.md) (blocked = HTTP 200, `blockReason` enum), [02-tech-stack.md](../02-tech-stack.md) (`ANTHROPIC_MAX_TOKENS`), [modules/chat-orchestrator/02-api-contracts.md](../modules/chat-orchestrator/02-api-contracts.md), [modules/chat-orchestrator/03-architecture.md](../modules/chat-orchestrator/03-architecture.md)

## Context

Прод-кейс (broadnova, 2026-06-10). Запрос «сделай лендинг» (`assistantMode=code`, project-сессия) → `POST /v1/chat/run` вернул `status=assistant_message`, `toolCall=null`, `usage.outputTokens=4096`. При этом тот же шаг в истории (`GET /v1/chats/{id}` → `steps[].payload`) несёт `text` + **два** блока `tool_use` (`files.write`) с **неполным** `input` (только `path`, тело файла обрезано). iOS-флоу завязан на `/chat/run`, поэтому клиент получил «финальный» ответ без инструментов и без признака обрезки — генерация лендинга молча провалилась.

Корень — **две независимые дефектные ветки** оркестратора.

### Проблема A — обрезка по `max_tokens`

1. `src/app/config.py:35`: `ANTHROPIC_MAX_TOKENS` default **4096**, в prod-`.env` не переопределён. Для генерации кода/файлов (несколько `files.write` с полным содержимым) 4096 output-токенов мало → Claude не успевает закрыть ход, ответ обрезается с `stop_reason="max_tokens"`.
2. `anthropic_client.create_message` — **НЕ** streaming, `max_tokens=self._max_tokens` (4096).
3. `orchestrator.py:511`: `if result.stop_reason == "tool_use" and result.tool_uses: …` → `_handle_tool_use`; **иначе** (`:529`) → `status="assistant_message"` + биллинг. При `stop_reason="max_tokens"` блоки `tool_use` в `content` **есть**, но `stop_reason != "tool_use"` → ход уходит в else → `assistant_message`, `toolCall=null`, обрезанные `tool_use` молча теряются для клиента (но персистятся в `chat_steps.payload` как **неполные** блоки).
4. Неполный `tool_use` (например `files.write` без `content`) **нельзя** исполнять — `input` невалиден; реплеить его в continuation тоже опасно (битый ход в истории Anthropic).

### Проблема B — параллельные client-side tool-вызовы

Claude в одном assistant-ходе может вернуть **несколько** `tool_use`-блоков (parallel tool use). Это уже поддержано на уровне хранения ([ADR-008](ADR-008-provider-tool-use-id.md): каждый блок → свой `tool_calls` со своим domain id + `provider_tool_use_id`), но **не** на уровне публичного ответа:

1. `orchestrator.py:682-733` (`_handle_tool_use`): цикл `for block in result.tool_uses` персистит `tool_calls` (status=pending) и audit для **каждого** client-side tool_use, но `first_client_out` присваивается **только первому** (`:730` `elif first_client_out is None`). `ChatResponse.toolCall` — **одиночный** → остальные client-side `tool_use` хода **не возвращаются** клиенту.
2. `/chat/tool-result` принимает **один** `toolCallId` + `result|error`.
3. Контракт Anthropic tool-loop: на **каждый** `tool_use` ассистент-хода в следующем витке `messages` обязан быть соответствующий `tool_result` (по `tool_use_id`), иначе → `400 invalid_request_error` → `502`. Поэтому одиночный `toolCall` на мульти-tool ходе **ломает** continuation: iOS физически не может прислать результаты по tool-вызовам, которых не видел, → следующий виток никогда не соберётся корректно.

Смешанный ход (server-side `site.*` + client-side в одном assistant-ходе) также не покрыт: server-side исполняется на бэке, но client-side опять surface'ится одиночно.

## Decision

Два связанных фикса (A и B). Оба — поведенческие + аддитивно-контрактные; биллинг ([ADR-006](ADR-006-credit-billing-and-subscription-grant.md)) и идентификаторы синка ([ADR-023](ADR-023-sync-ids-in-chat-response.md)) **не меняются**.

### A. max_tokens / обрезка

**A1. Поднять дефолт `ANTHROPIC_MAX_TOKENS` до `16000`.**

- Значение `16000` — компромисс «генерация кода/файлов без обрезки» vs «латентность и стоимость одного хода». Для лендинга/нескольких `files.write` 4096 заведомо мало; 16000 покрывает типовой генеративный ход с запасом, оставаясь в зоне приемлемой латентности non-streaming-вызова.
- **MVP остаётся non-streaming.** Anthropic SDK имеет timeout-гард: при больших `max_tokens` без streaming SDK предупреждает/ограничивает синхронный вызов (ориентир ~10 мин на очень больших значениях). `16000` — ниже порога, при котором SDK требует streaming; non-streaming `create_message` сохраняется (минимальная дельта, не трогаем tool-loop). Переход на streaming — отложен (см. [TD-018](../100-known-tech-debt.md)), фиксируется как путь при дальнейшем росте `max_tokens` или требовании прогрессивной отдачи.
- **Согласование таймаута.** При `max_tokens=16000` non-streaming текущий `ANTHROPIC_TIMEOUT_SECONDS=60` может оказаться мал для предельно длинного хода. Дефолт `ANTHROPIC_TIMEOUT_SECONDS` поднимается до **120** (2 мин) — конфигурируемо; значение остаётся существенно ниже SDK non-streaming-гарда. Это страхует от ложного `502` по таймауту на длинной генерации.
- **Per-instance в `.env`-контракте.** `ANTHROPIC_MAX_TOKENS` (и `ANTHROPIC_TIMEOUT_SECONDS`) фиксируются в `.env.example` / `.env.prod.example` и применяются к **каждому** инстансу мульти-инстанс-деплоя ([ADR-017](ADR-017-shared-server-traefik-deploy.md)): новый дефолт раскатывается на оба инстанса (`claude-ios`/broadnova и `avelyra`).
- **Биллинг не зависит от `max_tokens`:** 1 кредит = 1 message-step ([ADR-006](ADR-006-credit-billing-and-subscription-grant.md)) — длина/число output-токенов на стоимость не влияют.

**A2. Явная обработка `stop_reason="max_tokens"` — не отдавать молча обрезанный ответ.**

Когда `stop_reason="max_tokens"` (ход обрезан лимитом output-токенов), оркестратор **НЕ** трактует ход как финальный `assistant_message` и **НЕ** выдаёт неполные `toolCall`/`toolCalls` (их `input` неполон — исполнять нельзя). Вместо этого:

- Возвращается `status="blocked"` c **новым** `blockReason = "max_tokens"` (расширение enum [ADR-004](ADR-004-blocked-http-200.md)). HTTP `200` (бизнес-исход, не тех-ошибка) — единообразно с прочими blocked.
- Это **семантическое расширение** `blocked`: блокировка здесь — не policy-deny **до** генерации, а **обрыв генерации лимитом**. Поэтому для `blockReason="max_tokens"` `messageStepId`/`stepId` ведут себя **как при `assistant_message`** (ход и assistant-шаг **создаются** — Claude сгенерировал контент): оба **НЕ** null и указывают на ход/обрезанный assistant-шаг. Это отличает `max_tokens` от policy-blocked (где `messageStepId`/`stepId = null`, шаг не создавался, [ADR-023 §3](ADR-023-sync-ids-in-chat-response.md)). Признак «обрезано» виден клиенту по `blockReason="max_tokens"`.
- `usage` **присутствует** (в отличие от policy-blocked): отдаётся реальный usage обрезанного хода (`outputTokens` ≈ `max_tokens`) для диагностики. (Уточнение к [ADR-004](ADR-004-blocked-http-200.md): «`usage` нет при blocked» относится к policy-blocked до генерации; для `max_tokens`-blocked usage есть.)
- `assistantMessage` отдаётся, если в обрезанном ходе был `text`-блок (частичный текст) — клиент может показать «ответ оборван». Опционально/nullable.
- **`toolCall`/`toolCalls` НЕ отдаются** — обрезанные `tool_use` неполны.
- **Биллинг.** Обрезанный ход **НЕ** является успешным финальным `assistant_message` → **кредит не списывается** (debit только на `status=assistant_message`, [ADR-006](ADR-006-credit-billing-and-subscription-grant.md) / [03-architecture.md §8](../modules/chat-orchestrator/03-architecture.md)). Trial-flip также не выполняется. Пользователь не платит за оборванную генерацию.
- **Хранение/реплей.** Обрезанный assistant-шаг персистится в `chat_steps` (для истории/диагностики), но **обрезанные `tool_use`-блоки исключаются из continuation-реплея** — re-entry по этому ходу через `/chat/tool-result` для `max_tokens`-blocked не предусмотрен (исполнять нечего). Клиентский UX: повторить запрос (теперь с `max_tokens=16000` обрезка маловероятна) или разбить задачу.

**Рекомендация клиенту (iOS):** при `blockReason="max_tokens"` показать «ответ слишком длинный, повторите/сократите запрос». С новым дефолтом 16000 этот кейс становится редким — это safety-net, а не штатный путь.

### B. Параллельные client-side tool-вызовы

**B1. `/chat/run` и `/chat/tool-result`: возвращать ВСЕ client-side `tool_use` хода — `toolCalls[]`.**

В `ChatResponse` при `status="tool_call"` добавляется массив `toolCalls: [ToolCall]` — **все** client-side tool-вызовы текущего assistant-хода (в порядке блоков ответа Claude). Каждый элемент — та же форма `{ id (domain UUID), name (dot), args }`, что и одиночный `toolCall`.

- **Обратная совместимость.** Поле `toolCall` (одиночное) **сохраняется** и равно **первому** элементу `toolCalls[]` (`toolCalls[0]`). Старые клиенты, читающие только `toolCall`, продолжают работать для одиночного tool-вызова; для мульти-tool ходов они увидят первый (как сейчас), но **корректный** клиент обязан читать `toolCalls[]`. `toolCall` помечается **deprecated** (планируемое удаление — отдельным ADR после миграции iOS).
- **Server-side `site.*` в `toolCalls[]` НЕ попадают** ([ADR-011](ADR-011-server-side-tools.md)): исполняются на бэке немедленно, в публичный ответ как tool_call не выходят. `toolCalls[]` содержит **только** client-side (`files.*`/`calendar.*`/`reminders.*`).
- Каждый элемент `toolCalls[]` соответствует строке `tool_calls` (status=pending), уже персистированной в `_handle_tool_use`. domain id (`toolCall*.id`) — публичный ключ для `/chat/tool-result`; provider `toolu_...` наружу не отдаётся ([ADR-008](ADR-008-provider-tool-use-id.md)).
- `stepId` (ADR-023) — id **одного** assistant-шага, чей `payload` несёт **все** эти `tool_use`-блоки (parallel tool use = один assistant-шаг с несколькими блоками). Все элементы `toolCalls[]` принадлежат этому `stepId`; `messageStepId` — один ход. `assistantMessage` (ADR-024) — сопутствующий `text` того же шага.

**B2. `/chat/tool-result`: батч-приём результатов на ВСЕ tool-вызовы хода.**

`/chat/tool-result` принимает **массив** результатов:

```json
{
  "userId": "uuid",
  "sessionId": "uuid",
  "results": [
    { "toolCallId": "uuid", "result": { } },
    { "toolCallId": "uuid", "error": { "code": "string", "message": "string" } }
  ]
}
```

- В каждом элементе — ровно одно из `result`/`error` (как прежде, поэлементно).
- **Обратная совместимость.** Старая форма (одиночные `toolCallId` + `result|error` на верхнем уровне) **сохраняется** и эквивалентна `results=[{toolCallId, result|error}]` (батч из одного). Backend принимает обе формы; одиночная — deprecated, удаление — отдельным ADR.
- **Виток к Anthropic продолжается ТОЛЬКО когда для ВСЕХ client-side `tool_use` текущего assistant-хода собраны `tool_result`.** Пока не все результаты получены — backend не делает следующий `messages.create` (иначе Anthropic `400` на orphan `tool_use`). Это «барьер» хода.
  - **Рекомендуемый путь — один батч-запрос со всеми результатами** (соответствует «все результаты перед витком»): iOS исполняет все tool-вызовы хода и присылает их разом. Тогда барьер закрывается одним `/chat/tool-result`.
  - **Накопительный путь (поддерживается):** iOS может слать результаты по одному/частями (несколько `/chat/tool-result` одного хода). Backend атомарно отмечает соответствующие `tool_calls` completed/errored и **накапливает**; пока не все client-side tool-вызовы хода completed/errored — отвечает «ожидаются ещё результаты» (см. ниже), без вызова Anthropic. Когда последний результат закрывает барьер — делает continuation-виток и возвращает следующий шаг (`tool_call`/`assistant_message`/`blocked`).
- **Промежуточный ответ при незакрытом барьере.** Если после применения батча остаются client-side tool-вызовы хода без результата, `/chat/tool-result` возвращает `status="tool_call"` с `toolCalls[]` = **оставшиеся** (ещё не completed) client-side вызовы того же хода (и `toolCall` = первый из оставшихся). Это сообщает клиенту, какие результаты ещё ждут. Биллинг при этом не выполняется (не финальный шаг). `messageStepId` стабилен (тот же ход), `stepId` = id assistant-шага хода с `tool_use`-блоками.
- **Idempotency / повторы ([ADR-005](ADR-005-idempotency-ledger.md)).**
  - Повторный `toolCallId` со статусом `completed`/`errored` — **идемпотентен**: результат не перезаписывается, Anthropic повторно не вызывается. Если барьер уже был закрыт и continuation-шаг сохранён — возвращается тот же сохранённый следующий шаг.
  - Дубли внутри одного батча (один `toolCallId` дважды) — `422`.
  - `toolCallId` не из текущего хода/чужой сессии — `404`/`403` (как прежде).
  - `toolCallId`, относящийся к уже завершённому (другому) ходу — идемпотентный возврат сохранённого шага.
  - Continuation-виток к Anthropic выполняется **один раз** на закрытие барьера (защита `messageStepId`-идемпотентностью дебита на финальном шаге, [ADR-006](ADR-006-credit-billing-and-subscription-grant.md)).

**B3. Оркестратор.**

- `_handle_tool_use`: вместо `first_client_out` собирать **список** всех client-side `ToolCallOut` хода → `toolCalls[]`; `toolCall` = `toolCalls[0]`. Server-side `site.*` исполнять сразу (без изменений [ADR-011](ADR-011-server-side-tools.md)).
- **Смешанный ход** (server-side + client-side в одном assistant-ходе): server-side `site.*` исполнить немедленно, записать их `tool_result` (на бэке); client-side вернуть в `toolCalls[]` и ждать их результатов. Continuation-виток собирает `messages` со **всеми** `tool_result` хода (и server-side, и client-side) перед следующим `messages.create`. Реплей `tool_result` по `provider_tool_use_id` ([ADR-008](ADR-008-provider-tool-use-id.md)), порядок шагов по `seq` ([ADR-021](ADR-021-deterministic-step-order-and-block-normalization.md)).
- **Барьер хода** определяется так: для assistant-шага-хода (с `tool_use`-блоками) собрать множество client-side `tool_calls` этого `message_step_id`; continuation разрешён, когда все они в статусе completed/errored.
- `messageStepId`/`stepId` ([ADR-023](ADR-023-sync-ids-in-chat-response.md)): `stepId` хода — один (assistant-шаг с несколькими `tool_use`); все `toolCalls[]` из этого шага; `messageStepId` стабилен на весь ход и все его `/chat/tool-result`.
- Нормализация истории ([ADR-024](ADR-024-history-payload-domain-normalization.md)) уже отдаёт **все** блоки шага (включая несколько `tool_use`) с доменными именами/id — согласуется с `toolCalls[]` (инвариант: `toolCalls[i].name/.id` == соответствующий `tool_use`-блок шага в истории == `/v1/tools` `name`).

**B4. Биллинг — без изменений.** 1 кредит = 1 message-step ([ADR-006](ADR-006-credit-billing-and-subscription-grant.md)); списание один раз на финальном `assistant_message` хода, **не** на каждый tool и **не** на каждый `/chat/tool-result`. Несколько параллельных tool-вызовов и батч-результаты остаются в рамках одного хода = один кредит.

## Rationale

- **Fix A (max_tokens):** молчаливая потеря обрезанных `tool_use` — худший UX (генерация «успешна», но пустая). Поднятие дефолта устраняет частую причину; явный `blockReason="max_tokens"` делает редкий остаточный кейс наблюдаемым и не-биллящимся. non-streaming сохраняется ради минимальной дельты на MVP; streaming — осознанно отложен в [TD-018](../100-known-tech-debt.md).
- **Fix B (parallel tools):** одиночный `toolCall` на мульти-tool ходе нарушает контракт Anthropic tool-loop (orphan `tool_use` → 400) — это функциональный дефект continuation, а не косметика. `toolCalls[]` + батч `/chat/tool-result` — прямое следствие требования «все `tool_result` перед витком». Сохранение одиночных `toolCall`/`toolCallId` — обратная совместимость с уже задеплоенным iOS.
- **Почему `blocked` (а не новый top-level `status` `truncated`):** `blocked` уже = «генерация не дала нормального результата, HTTP 200, машиночитаемая причина» ([ADR-004](ADR-004-blocked-http-200.md)). Расширение enum `blockReason` дешевле для клиента (уже обрабатывает `blocked`+`blockReason`), чем новый `status`. Отличие от policy-blocked (наличие `usage`/`stepId`) задокументировано.
- **Почему батч (а не только по одному):** «все результаты перед витком» — естественная единица. Батч закрывает барьер одним запросом и минимизирует round-trips; накопительный путь оставлен для гибкости iOS, но рекомендуется батч.

## Consequences

- **Положительные:** генерация кода/файлов перестаёт молча обрезаться; обрезка наблюдаема и бесплатна для пользователя; parallel tool use корректно проходит continuation; обратная совместимость со старым iOS сохранена.
- **Издержки / обязательства backend:**
  - `ChatResponse`: добавить `toolCalls: list[ToolCall] | None`; `toolCall` остаётся (= `toolCalls[0]`, deprecated).
  - `ChatToolResultRequest`: принимать `results: [{toolCallId, result|error}]`; старая одиночная форма — алиас на батч из одного.
  - Оркестратор: surface всех client-side tool_use; барьер хода (continuation только при всех собранных результатах); смешанный ход.
  - `BlockReason` enum: добавить `max_tokens`; ветка `stop_reason="max_tokens"` → `blocked(max_tokens)` с `usage`/`messageStepId`/`stepId`, без биллинга, без выдачи неполных `toolCall(s)`.
  - Config/env: `ANTHROPIC_MAX_TOKENS=16000`, `ANTHROPIC_TIMEOUT_SECONDS=120` (дефолты в `config.py` + `.env*`), per-instance.
- **Обязательства devops:** обновить `ANTHROPIC_MAX_TOKENS`/`ANTHROPIC_TIMEOUT_SECONDS` в `.env` обоих инстансов (broadnova, avelyra) при выкатке; новый дефолт применяется к каждому инстансу.
- **Тестовые требования (нормативно):** см. [modules/chat-orchestrator/09-testing.md §Parallel tool calls + max_tokens (ADR-025)](../modules/chat-orchestrator/09-testing.md).

## Alternatives

- **A: новый top-level `status="truncated"`** — отклонён: дороже для клиента (новый дискриминатор), `blocked`+`blockReason` уже покрывает «нет нормального результата».
- **A: автоматически продолжать обрезанный ход (continue)** — отклонён: Anthropic non-streaming не даёт чистого «дописать с места»; обрезанные `tool_use` неполны, реплей опасен (битый ход). Повтор/сокращение запроса проще и предсказуемее. Возможный путь — streaming + partial tool_use accumulation ([TD-018](../100-known-tech-debt.md)).
- **A: молча исполнять первый полный tool_use из обрезанного хода** — отклонён: нельзя гарантировать полноту даже первого блока при `max_tokens`; молчаливое поведение — исходный дефект.
- **B: заменить `toolCall` на `toolCalls[]` без сохранения одиночного** — отклонён: breaking change для задеплоенного iOS. Сохраняем оба, `toolCall` deprecated.
- **B: `/chat/tool-result` строго по одному (без батча)** — отклонён как единственная форма: не соответствует «все результаты перед витком», лишние round-trips. Накопительный путь оставлен как опция, батч — рекомендуемый.
- **B: continuation после первого результата (как сейчас, неявно)** — отклонён: orphan `tool_use` → Anthropic 400 (исходный дефект).
