# ADR-011 — Server-side tools (`site.*`): backend исполняет, не отдаёт клиенту

- Статус: Accepted
- Дата: 2026-06-01
- Связан с: [ADR-008](ADR-008-provider-tool-use-id.md), [ADR-010](ADR-010-backend-hosted-preview.md), [modules/chat-orchestrator/02-api-contracts.md](../modules/chat-orchestrator/02-api-contracts.md), [modules/website-builder/](../modules/website-builder/README.md)

## Context

Существующий tool-протокол ([modules/chat-orchestrator/02-api-contracts.md](../modules/chat-orchestrator/02-api-contracts.md))
устроен как **client-side**: backend только **инициирует** tool-call (отдаёт `status=tool_call` с `toolCall{id,name,args}`),
а исполняет его **iOS-клиент** локально (`files.*`, `calendar.*`, `reminders.*`) и присылает `tool_result` через
`POST /v1/chat/tool-result`. Backend сам ничего не исполняет.

Website-builder вводит tools, которые работают с **backend-хранилищем** проекта (`projects`/`site_files`, [ADR-010](ADR-010-backend-hosted-preview.md)):
запись файла сайта, выдача превью-URL и т.п. Эти действия **не имеют смысла на iOS** — данные и логика живут на backend.
Отдавать такой tool-call клиенту (как `files.*`) было бы неверно: клиенту нечего исполнять, и это лишний round-trip.

Нужно ввести категорию **server-side tools** и явно зафиксировать, чем они отличаются от client-side, как orchestrator
их различает и как они вписываются в tool-loop и биллинг.

## Decision

Вводится класс **server-side tools** с доменным префиксом `site.*`. Принцип (нормативно):

> **`files.*` / `calendar.*` / `reminders.*` → исполняет iOS-клиент** (client-side): backend возвращает
> `status=tool_call`, ждёт `tool_result`.
> **`site.*` → исполняет backend** (server-side): backend выполняет действие в своём хранилище **немедленно**,
> в том же tool-loop, формирует `tool_result` **сам** и продолжает цикл к Anthropic **без round-trip к iOS**.
> Server-side tool-call **НЕ** отдаётся клиенту как `status=tool_call`.

### 1. Различение orchestrator'ом

- Принадлежность tool к классу определяется по доменному имени через **статический реестр** (как и существующий
  domain↔anthropic mapping, [ADR-008](ADR-008-provider-tool-use-id.md)): множество `SERVER_SIDE_TOOLS = {site.write_file, site.preview, site.list, site.read, site.delete}`.
- В tool-loop, получив от Claude `tool_use`:
  - **client-side** (`files.*`/`calendar.*`/`reminders.*`) → как сейчас: персист `tool_calls`, ответ `status=tool_call` клиенту,
    ожидание `/chat/tool-result`.
  - **server-side** (`site.*`) → backend исполняет хэндлер **синхронно** в рамках обработки `/chat/run` (или продолжения),
    формирует `tool_result` (или `is_error=true`), сразу делает следующий `messages.create` к Anthropic в том же шаге,
    **не выходя** к клиенту. Цикл повторяется, пока Claude не вернёт `assistant_message` (финал шага) или client-side tool.
- Смешанные шаги допустимы: в одном message-шаге могут чередоваться server-side (исполняются на backend) и client-side
  (уходят на iOS) tools. `messageStepId` един на весь шаг (ADR-006); биллинг — 1 кредит на финальный assistant_message,
  server-side раунды кредитов не списывают (как и client-side tool-раунды).

### 2. Tool-loop guard

- Чтобы server-side петля не зациклилась, вводится лимит последовательных server-side раундов на один message-шаг
  `MAX_SERVER_TOOL_ROUNDS` (env, дефолт 16). Превышение → ошибка обработки шага (`502`/контролируемый отказ), audit-запись.

### 3. Имена и согласованность с Anthropic (повтор инвариантов ADR-008)

- domain↔anthropic mapping расширяется server-side tools: `site.write_file ↔ site_write_file`, `site.preview ↔ site_preview`,
  `site.list ↔ site_list`, `site.read ↔ site_read`, `site.delete ↔ site_delete` (точка → подчёркивание, статическая таблица).
- `provider_tool_use_id` (`toolu_...`) хранится и для server-side tools (ADR-008): backend строит `tool_result.tool_use_id`
  из него при продолжении, история реплеится дословно. То, что исполнение локально на backend, не меняет требований Anthropic
  к согласованности `tool_use.id`.

### 4. Аудит и мутирование

- `site.write_file` и `site.delete` ∈ **MUTATING** → audit-запись (`tool_mutation`, как `files.write` и пр.).
- `site.read`/`site.list`/`site.preview` — read/utility, без обязательного audit мутации (`site.preview` выдаёт временный
  доступ — логируется на уровне tool lifecycle, но не как мутация хранилища).
- Server-side tool-call логируется в `tool_calls` (для трассировки и идемпотентности re-entry в рамках шага), но **без**
  ожидания client `tool_result`: `status` сразу `completed` с backend-результатом.

### 5. Строгая типизация

- args/result server-side tools — строгие Pydantic v2 схемы (`extra='forbid'`), как у client-side. Схемы — в
  [modules/website-builder/02-api-contracts.md](../modules/website-builder/02-api-contracts.md) и в каталоге tools
  chat-orchestrator (раздел server-side).

## Consequences

**Положительные:**
- Чёткая, явная граница «кто исполняет tool»: префикс `site.*` = backend, остальное = iOS. Нет двусмысленности.
- Нет лишнего round-trip к iOS для backend-операций; tool-loop эффективнее.
- Переиспользует существующие инварианты (mapping, provider_tool_use_id, messageStepId, биллинг) без их слома.
- Расширяемо: новые server-side классы добавляются в реестр + mapping.

**Отрицательные / ограничения:**
- Tool-loop усложняется ветвлением client-side/server-side; нужен guard на число server-side раундов (см. §2).
- Server-side исполнение в синхронном пути `/chat/run` удлиняет один HTTP-запрос (несколько Anthropic-раундов подряд).
  Приемлемо при лимите раундов; при росте — вынести в стрим/async (будущий TD, не на старте).

## Alternatives

1. **Отдавать `site.*` клиенту как client-side и просить iOS дёргать backend-эндпоинт.** Отвергнуто: бессмысленный
   round-trip, iOS становится прокси к backend-хранилищу, дублирование логики и контрактов.
2. **Отдельный не-tool API для генерации сайта (вне chat-loop).** Отвергнуто: теряется агентность Claude (модель
   сама решает, когда писать файлы в ходе диалога); генерация — обычный chat-шаг (ADR-006, биллинг не меняется).
3. **Различать server/client по флагу в args, а не по имени.** Отвергнуто: имя/реестр — единственный источник истины
   (как domain↔anthropic mapping), флаг в args подделываем моделью и размывает контракт.
