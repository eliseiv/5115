# ADR-024 — Нормализация content-блоков истории к доменному виду при отдаче `GET /v1/chats/{id}`

- Статус: Accepted
- Дата: 2026-06-10
- Связан с: [ADR-008](ADR-008-provider-tool-use-id.md) (provider `tool_use.id` vs доменный `toolCall.id`; dot↔underscore имена / BUG-3/BUG-4), [ADR-021](ADR-021-deterministic-step-order-and-block-normalization.md) (нормализация блоков **перед персистом**, порядок `seq`), [ADR-023](ADR-023-sync-ids-in-chat-response.md) (`messageStepId`/`stepId` в `ChatResponse`), [ADR-011](ADR-011-server-side-tools.md) (server-side tool-loop), [modules/chats/02-api-contracts.md](../modules/chats/02-api-contracts.md), [modules/chat-orchestrator/02-api-contracts.md](../modules/chat-orchestrator/02-api-contracts.md), [modules/chat-orchestrator/04-data-model.md](../modules/chat-orchestrator/04-data-model.md)

## Context

iOS-разработчик не может синхронизировать представление чата между генерацией и историей. Корень один: **`GET /v1/chats/{id}` отдаёт `steps[].payload` как СЫРОЙ Anthropic wire-content**, тогда как `/v1/chat/run` (`ChatResponse`) и `/v1/tools` отдают **доменный** дискриминированный вид. Это даёт три связанные нестыковки.

### Нестыковка 1 — имя инструмента (dot vs underscore)
В истории `payload.content[].name` для `tool_use`-блока — это **anthropic-формат с подчёркиванием** (`calendar_create_events`), потому что хранится дословный wire-блок ответа Claude (`to_anthropic_tool_name`, `tools.py:336`). А `/chat/run` `toolCall.name`, `/v1/chats/{id}/steps` `toolName` и `/v1/tools` `name` отдают **доменный формат с точкой** (`calendar.create_events`). Клиент не может сопоставить инструмент по имени.

### Нестыковка 2 — id инструмента (provider vs domain)
В истории `payload.content[].id` (`tool_use`) и `payload.content[].tool_use_id` (`tool_result`) — это **провайдерский** `toolu_...` (`orchestrator.py:685`), сохранённый дословно ради реплея ([ADR-008](ADR-008-provider-tool-use-id.md)). А `/chat/run` `toolCall.id` — **доменный** `uuid4` (= `tool_calls.id`). Клиент не может сопоставить tool-вызов из истории с tool-вызовом из ответа генерации (и с `/chat/tool-result`-роутингом).

### Нестыковка 3 — дискриминация vs полнота
Один assistant-ход Claude может нести **в одном сообщении** и `text`, и `tool_use` (Anthropic возвращает массив блоков). В истории оба блока хранятся в одном шаге (`payload.content = [text, tool_use]`). А `ChatResponse` отдаёт **одно** дискриминированное состояние: при `status=tool_call` исходно возвращался только `toolCall`, сопутствующий текст **отбрасывался** (`orchestrator.py:661`); при `status=assistant_message` — только `assistantMessage`. Клиент не мог свести «урезанный» ответ run с «полным» шагом истории. *(Часть с потерей текста при `tool_call` устранена решением [Q-024-1](../99-open-questions.md) = вариант A — см. §Decision п.3: `assistantMessage` теперь опционально возвращается и при `tool_call`.)*

### Ключевой инвариант хранения (не нарушать)
`chat_steps.payload` ДОЛЖЕН оставаться wire-валидным для реплея в Claude (`_build_messages`): `tool_use.name` — underscore, `tool_use.id`/`tool_result.tool_use_id` — provider `toolu_...` (инварианты [ADR-008](ADR-008-provider-tool-use-id.md): пара id в истории Anthropic согласована по построению; [ADR-021](ADR-021-deterministic-step-order-and-block-normalization.md): нормализация **перед персистом** убирает только не-wire SDK-поля типа `caller`). Поэтому **трогать хранение нельзя** — нормализация к доменному виду возможна **только на границе сериализации ответа истории**.

### Что уже доступно для переиспользования
- `to_domain_tool_name()` (`tools.py:104`) — обратный маппинг underscore→dot (тот же, что применяется при парсинге ответа Claude в `toolCall.name`).
- Таблица `tool_calls` связывает `provider_tool_use_id ↔ id` (доменный) 1:1 в пределах сессии ([ADR-008](ADR-008-provider-tool-use-id.md)); доступны `repo.get_tool_call` / выборка по `session_id`.
- `GET /v1/chats/{id}/steps` (steps-view) **уже** отдаёт доменное dot-`toolName` и не утекает provider id — то есть доменная проекция истории уже существует, но только для UI-вида, без `payload`/`id`.

## Decision

**Полная нормализация `steps[].payload` к доменному виду при отдаче `GET /v1/chats/{id}` — только на границе сериализации ответа. Хранение и реплей не меняются.**

### 1. Нормализация имени (нестыковка 1)
Для каждого блока `payload.content[]` с `type == "tool_use"`: поле `name` (underscore) → доменное dot-имя через `to_domain_tool_name()`. Неизвестное имя (нет в маппинге) — трактуется как upstream-аномалия: блок не должен попадать в историю в нормальном потоке; при обнаружении возвращается как есть (имя без преобразования) и логируется warning — не 500 на чтении истории (история read-only, отказоустойчива). Текстовые блоки (`type == "text"`) и любые иные поля `tool_use` (`input`) — **не меняются**.

### 2. Нормализация id (нестыковка 2)
Backend строит для сессии **одну** карту `provider_tool_use_id → domain tool_call_id` одним запросом (`SELECT id, provider_tool_use_id FROM tool_calls WHERE session_id = :s`) — без N+1. Затем для каждого блока `payload.content[]`:
- `type == "tool_use"`: `id` (`toolu_...`) → доменный `tool_calls.id` по карте.
- `type == "tool_result"`: `tool_use_id` (`toolu_...`) → доменный `tool_calls.id` по той же карте.

После нормализации provider id (`toolu_...`) **наружу в истории не утекает никогда** (симметрично [ADR-008](ADR-008-provider-tool-use-id.md): provider id — внутренний; и steps-view, который его и так не отдавал).

**Отсутствие записи в карте** (provider id, для которого нет `tool_calls`-строки — теоретически server-side `site.*` блоки, если для них не создаётся `tool_calls`): блок отдаётся с исходным id и логируется warning. Backend при реализации проверяет, создаётся ли `tool_calls` для server-side tool-раундов; если нет — это отдельный пробел маппинга, фиксируется как уточнение (см. next_steps), но не блокирует нормализацию client-side tool-вызовов.

### 3. Полнота шага (нестыковка 3) — история отдаёт ВСЕ блоки шага
**Подтверждается фактическое поведение:** assistant-шаг в истории МОЖЕТ содержать `payload.content = [text, tool_use]` (или несколько `tool_use` при parallel tool use) в одном шаге. История **сохраняет полный список блоков шага** — это и есть «полнота», которой нет в дискриминированном `ChatResponse`.

**Нормативный контракт чтения для клиента:** канонический источник полного хода — `GET /v1/chats/{id}` → `steps[].payload.content[]` (полный, упорядоченный массив доменно-нормализованных блоков). `ChatResponse` — это **прогресс-проекция** (одно дискриминированное состояние текущего раунда для оптимистичного UI), а не полный ход. Клиент склеивает прогресс-ответ с полным шагом истории по `stepId`/`messageStepId` ([ADR-023](ADR-023-sync-ids-in-chat-response.md)): `ChatResponse.stepId` == `steps[].id` шага-носителя, а полный набор блоков (включая сопутствующий текст при tool_use) клиент берёт из `steps[].payload`.

**Продуктовый инкремент — [Q-024-1](../99-open-questions.md) Closed (2026-06-10, решение пользователя = вариант A):** `/chat/run` и `/chat/tool-result` при `status=tool_call` **ТАКЖЕ опционально возвращают** сопутствующий `assistantMessage` — текст из `text`-блоков **того же** assistant-шага, чей `tool_use` вернулся как `toolCall` (тот шаг, на который указывает `stepId`, [ADR-023](ADR-023-sync-ids-in-chat-response.md)). При отсутствии текста — `assistantMessage = null`/опущено. Backend перестаёт отбрасывать текст (`orchestrator.py:661`) и кладёт его в `assistantMessage`. Изменение аддитивно/обратносовместимо (поле уже опционально-nullable; новизна — может быть НЕ-null при `tool_call`); `toolCall` остаётся обязательным при `tool_call`. **Согласование:** `assistantMessage` при `tool_call` == текст `text`-блоков того же шага в истории (`steps[].payload.content[]` по `stepId` — нормализация текстовые блоки не трогает), поэтому run-проекция и история несут один и тот же сопутствующий текст. Контракт зафиксирован в [chat-orchestrator/02-api-contracts.md §Response (200)](../modules/chat-orchestrator/02-api-contracts.md#response-200). Нестыковки 1 и 2 закрываются нормализацией истории; нестыковка 3 теперь закрыта с обеих сторон — и в истории (полный шаг по `stepId`), и в `ChatResponse` (`assistantMessage` при `tool_call`).

### Инвариант синка (нормативно, расширяет [ADR-023](ADR-023-sync-ids-in-chat-response.md))
В ответе `GET /v1/chats/{id}` для любого `tool_use`/`tool_result`-блока `steps[].payload.content[]`:
- `tool_use.name` (dot) **дословно равен** `/chat/run` `toolCall.name`, `/v1/chats/{id}/steps` `toolName` и `/v1/tools` `name` того же инструмента;
- `tool_use.id` (domain UUID) **дословно равен** `/chat/run` `toolCall.id` (= `tool_calls.id`) соответствующего вызова;
- `tool_result.tool_use_id` (domain UUID) **дословно равен** тому же `tool_calls.id`, что и `tool_use.id` породившего вызова;
- текстовые блоки (`type == "text"`) и `tool_use.input` — байт-в-байт как в хранилище (не модифицируются);
- provider `tool_use.id` (`toolu_...`) **отсутствует** в любом блоке ответа истории.

### Что НЕ меняется
- **Хранение** `chat_steps.payload` — без изменений (wire-валидно, underscore-имена, provider id; нормализация перед персистом [ADR-021] не затрагивается).
- **Реплей** `_build_messages` к Anthropic — без изменений (читает хранилище дословно; пары id согласованы по [ADR-008](ADR-008-provider-tool-use-id.md)).
- **Миграции, data-model** — без изменений (нормализация чисто на слое сериализации ответа; нет нового состояния, нет колонок).
- **`ChatResponse`** (`/chat/run`, `/chat/tool-result`) — дискриминирующая структура без изменений (`toolCall.name`/`toolCall.id` уже доменные; `messageStepId`/`stepId` — [ADR-023](ADR-023-sync-ids-in-chat-response.md)). **Единственное аддитивное расширение (Q-024-1, вариант A):** `assistantMessage` теперь может быть НЕ-null при `status=tool_call` (текст того же шага, см. §Decision п.3). Поле уже было опционально-nullable — обратносовместимо.
- **`GET /v1/chats/{id}/steps`** (steps-view) — без изменений (уже доменное `toolName`, без id/payload).
- Security, HTTP-коды, пути, request-схемы, биллинг, policy, idempotency — без изменений.

## Rationale

### Почему нормализация на отдаче, а не в хранении
Хранение — единственный источник для реплея в Claude и обязано быть wire-валидным (underscore-имена, provider id), иначе ломается continuation ([ADR-008](ADR-008-provider-tool-use-id.md)/[ADR-021](ADR-021-deterministic-step-order-and-block-normalization.md), BUG-3/BUG-4/BUG-5 — уже исправленные регрессы). Доменное представление — публичный контракт. Это два разных вида одних данных; единственное корректное место преобразования — **граница сериализации ответа истории**, симметрично тому, как `toolCall.name` уже нормализуется при парсинге ответа Claude (та же функция `to_domain_tool_name`).

### Почему переиспользуем существующий маппинг, а не новый
`to_domain_tool_name()` и таблица `tool_calls` (`provider_tool_use_id ↔ id`) — уже единственные источники истины для dot↔underscore и provider↔domain ([ADR-008](ADR-008-provider-tool-use-id.md)). Любая параллельная логика рисковала бы разойтись. Нормализация истории обязана использовать ровно их — тогда инвариант «имя/id в истории == в `/chat/run`/`/v1/tools`» выполняется по построению.

### Почему карту строим одним запросом на сессию
N+1 (`get_tool_call` на каждый блок) недопустим для истории с многораундовым tool-loop. Один `SELECT ... WHERE session_id=:s` даёт полную карту `provider→domain` для всех блоков всех шагов сессии — O(1) запросов на отдачу истории.

### Почему полнота — на стороне истории, а дискриминация — на стороне run
`ChatResponse` по своей природе пошаговый прогресс-протокол (`status` дискриминирует одно состояние раунда для немедленного UI). История — каноническое полное представление. Делать `ChatResponse` «полным» (массив блоков) — это переписать tool-loop-контракт; вместо этого фиксируем, что полный ход всегда доступен в истории по `stepId`, а необходимость дотянуть `ChatResponse` (сопутствующий текст при tool_call) — отдельный продуктовый инкремент (Q-024-1), не ломающий синк.

### Q-024-1 — решение (вариант A, 2026-06-10)
- **Вариант A (выбран) — добавить опц. `assistantMessage` в ответ `tool_call`.** Плюсы: полная консистентность run↔история (клиенту не нужен второй запрос истории, чтобы показать «Claude сказал X и вызвал инструмент Y»); сопутствующий текст перестаёт теряться в прогресс-UI; аддитивно/обратносовместимо (поле уже опционально в схеме). Издержка: backend перестаёт отбрасывать текст при `tool_call` (`orchestrator.py:661`) и кладёт его в `assistantMessage`.
- **Вариант B (отклонён) — оставить как есть, документировать «полный ход — в истории по `stepId`».** Минусы: прогресс-UI не покажет сопутствующий текст без запроса истории; UX-задержка/доп. запрос.
- **Решение: A** (пользователь, 2026-06-10) — полная консистентность без второго round-trip, существующие поля не ломаются. Контракт зафиксирован в §Decision п.3 и [chat-orchestrator/02-api-contracts.md](../modules/chat-orchestrator/02-api-contracts.md#response-200); тест-требование — [chat-orchestrator/09-testing.md §ADR-024](../modules/chat-orchestrator/09-testing.md).

## Consequences

- **Положительные:** iOS детерминированно сводит историю с `/chat/run`/`/v1/tools` по имени (dot) и id (domain UUID); provider id больше не утекает в историю (усиление [ADR-008](ADR-008-provider-tool-use-id.md)); полный ход (text+tool_use одного шага) читается из `steps[].payload`; хранение/реплей/миграции не тронуты (не breaking для continuation).
- **Издержки / обязательства backend:** при сериализации ответа `GET /v1/chats/{id}` (chats router/service/repository) — построить карту `provider_tool_use_id→domain id` сессии одним запросом, применить `to_domain_tool_name` к `tool_use.name` и подмену id к `tool_use.id`/`tool_result.tool_use_id` для каждого блока; текстовые блоки не трогать. Нормализация **на копии** payload для ответа — оригинал хранилища неизменен.
- **Согласование с ADR-008/BUG-3/BUG-4:** нормализация **не нарушает** их — внутри (хранение/реплей) всё по-прежнему underscore + provider id; меняется только публичная сериализация ответа истории. Связь 1:1 `provider↔domain` ([ADR-008](ADR-008-provider-tool-use-id.md)) — ровно тот источник, по которому подменяется id. dot↔underscore (BUG-3) — ровно та функция `to_domain_tool_name`, что уже применяется к `toolCall.name`.
- **Тестовое требование (нормативно):** см. [modules/chat-orchestrator/09-testing.md](../modules/chat-orchestrator/09-testing.md) раздел «История — доменная нормализация payload (ADR-024)» и [modules/chats/09-testing.md](../modules/chats/09-testing.md). Покрытие: история отдаёт dot-имя == `/v1/tools`/`toolCall.name`; `tool_use.id` == `/chat/run` `toolCall.id` того же вызова; `tool_result.tool_use_id` == тот же домен id; текстовые блоки целы (байт-в-байт); шаг с `[text, tool_use]` отдаётся полностью (оба блока); provider `toolu_...` отсутствует в ответе истории; хранилище (`chat_steps.payload`) после отдачи истории по-прежнему содержит underscore-имя и provider id (нормализация не мутировала хранение).

## Alternatives

- **Нормализовать в хранении (переписать payload на dot+domain id)** — отклонён: ломает реплей в Claude (underscore обязателен, BUG-3; provider id обязателен для согласованности пары tool_use/tool_result, BUG-4). Потребовал бы обратной денормализации в `_build_messages` — та самая хрупкая трансформация на hot path, отклонённая в [ADR-008](ADR-008-provider-tool-use-id.md) (вариант «б»).
- **Не нормализовать историю; клиент сам маппит underscore→dot и provider→domain** — отклонён: клиент не имеет доступа к таблице `tool_calls` (provider→domain — серверная связь); дублирование маппинга имён на клиенте — риск рассинхрона при изменении набора tools.
- **Отдавать в истории и raw, и domain (оба id/имени)** — отклонён: утечка provider id наружу (нарушает [ADR-008](ADR-008-provider-tool-use-id.md) «provider id — внутренний»); раздувает контракт; клиенту не нужен provider id.
- **Решить Q-024-1 без участия пользователя** — отклонён исходно: расширение публичного `ChatResponse` — продуктовый выбор пользователя; ADR зафиксировал нормализацию истории (нестыковки 1, 2 и полнота на стороне истории — нестыковка 3), а enrichment `ChatResponse` вынес в [Q-024-1](../99-open-questions.md). **Q-024-1 закрыт пользователем (2026-06-10) = вариант A** — enrichment `ChatResponse` теперь часть контракта (§Decision п.3).
