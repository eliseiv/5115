# ADR-026 — Project-independent (global) server-side tools + инструмент `time.now`

- Статус: Accepted
- Дата: 2026-06-10
- Связанные: [ADR-011](ADR-011-server-side-tools.md) (server-side `site.*`, project-scoped), [ADR-022](ADR-022-optional-project-and-tool-gating.md) (опциональный `projectId`, гейтинг `site.*`), [ADR-019](ADR-019-tools-catalog-endpoint.md) (каталог `/v1/tools`), [ADR-006](ADR-006-credit-billing-and-subscription-grant.md) (биллинг 1 кредит = 1 сообщение), [ADR-012](ADR-012-assistant-mode-vs-billing-mode.md) (assistant_mode), [Q-012-1](../99-open-questions.md) (фильтр tools по режиму)

## Контекст

**Проблема (репорт iOS-тестировщика).** В «чистом чате» (`/v1/chat/run`, режим chat/code) модель отвечает «сейчас 2024 год», хотя реальная дата — 2026-06-10. Причина: системный промт сервиса (`src/app/chat/orchestrator.py`, `_SYSTEM_PROMPT_CHAT`/`_SYSTEM_PROMPT_CODE`) **статичен** и не содержит даты; LLM без данных о времени в контексте угадывает дату по корпусу обучения и ошибается.

**Решение пользователя (согласовано, не пересматривается):** не хардкодить дату в промт, а дать модели **server-side инструмент `time.now`**, который исполняет backend (как `site.*`), без round-trip к iOS; модель получает дату только из результата вызова. В системный промт добавляется **generic-инструкция** (статичная, без даты): «у тебя нет встроенного знания текущей даты/времени; если она нужна — вызови `time.now`».

**Архитектурное ограничение (выявлено разведкой кода).** Существующий класс server-side tools (`site.*`, [ADR-011](ADR-011-server-side-tools.md)) **жёстко привязан к проекту website-builder**:

- `anthropic_tool_definitions(include_server_side=has_project)` — `SERVER_SIDE_TOOLS` (`site.*`) предлагаются Claude **только** при наличии `chat_sessions.project_id` ([ADR-022](ADR-022-optional-project-and-tool-gating.md));
- `_handle_tool_use` содержит инвариант-guard `assert external_project_id is not None` для любого tool из `SERVER_SIDE_TOOLS`, а исполнение делегируется `SiteToolHandlers.execute(..., external_project_id=...)` — резолв проекта обязателен.

`time.now` **обязан работать ВСЕГДА**, в основном flow чат-агрегатора **без проекта** ([ADR-022](ADR-022-optional-project-and-tool-gating.md): «чистый чат» = `project_id IS NULL`). Значит существующий project-scoped механизм `site.*` для `time.now` **не подходит**: он либо не предложит tool (нет проекта), либо упрётся в `assert external_project_id is not None`.

Вывод: нужен **новый класс server-side tools — project-independent (global)** — отдельный от project-scoped `SERVER_SIDE_TOOLS`, с маршрутизацией исполнения **до** project-scoped ветки и **без** резолва `external_project_id`.

## Решение

### 1. Три класса инструментов (формализация)

Вводится явная трёхклассовая классификация инструментов backend. Принцип разделения:

| Класс | Примеры | Кто исполняет | Требует `project_id`? | Предлагается Claude |
|---|---|---|---|---|
| **client-side** | `files.*`, `calendar.*`, `reminders.*` | **iOS-клиент** (round-trip через `/chat/tool-result`) | нет | по правилам assistant_mode ([Q-012-1](../99-open-questions.md)) |
| **server-side, project-scoped** | `site.*` (`SERVER_SIDE_TOOLS`) | **backend** в tool-loop | **да** (website-builder) | только при `project_id IS NOT NULL` ([ADR-022](ADR-022-optional-project-and-tool-gating.md)) |
| **server-side, global** (НОВЫЙ) | `time.now` (`GLOBAL_SERVER_SIDE_TOOLS`) | **backend** в tool-loop | **нет** | **ВСЕГДА** (включая «чистый чат» без проекта) |

`time.now` — server-side (исполняет backend, нет hand-off к iOS, как `site.*`), но **global** (нет привязки к проекту, доступен в любом ходе).

### 2. Новый реестр `GLOBAL_SERVER_SIDE_TOOLS`

В `src/app/chat/tools.py` вводится **отдельный** frozenset `GLOBAL_SERVER_SIDE_TOOLS = {time.now}`, **не пересекающийся** с project-scoped `SERVER_SIDE_TOOLS` (`site.*`).

Нормативные инварианты реестров:
- `GLOBAL_SERVER_SIDE_TOOLS ∩ SERVER_SIDE_TOOLS = ∅` (классы взаимоисключающие).
- Совокупность server-side = `SERVER_SIDE_TOOLS ∪ GLOBAL_SERVER_SIDE_TOOLS`; всё остальное в `ALL_TOOL_NAMES` — client-side.
- `time.now` добавляется в `ALL_TOOL_NAMES`, `_DOMAIN_TO_ANTHROPIC`/`_ANTHROPIC_TO_DOMAIN` (`time.now ↔ time_now`, та же dot→underscore схема, BUG-3), `_ARGS_BY_TOOL`, `TOOL_DESCRIPTIONS`.
- `time.now` **НЕ** входит в `MUTATING_TOOLS` (read-only, нет audit мутации).
- В каталоге `GET /v1/tools` ([ADR-019](ADR-019-tools-catalog-endpoint.md)) `time.now` → `execution: "server"`, `mutating: false` (поле `execution` уже = `"server" if name in SERVER_SIDE_TOOLS else "client"` — **обновляется** на `"server" if name in (SERVER_SIDE_TOOLS ∪ GLOBAL_SERVER_SIDE_TOOLS) else "client"`).

### 3. `anthropic_tool_definitions` — `time.now` предлагается ВСЕГДА

Текущая сигнатура: `anthropic_tool_definitions(*, include_server_side: bool = True)` — флаг `include_server_side` исключает `SERVER_SIDE_TOOLS` (`site.*`) при `project_id IS NULL`.

**Изменение (нормативно):** флаг `include_server_side` продолжает гейтить **только** project-scoped `SERVER_SIDE_TOOLS` (`site.*`). Tools из `GLOBAL_SERVER_SIDE_TOOLS` (`time.now`) **никогда** не исключаются этим флагом — они предлагаются Claude независимо от наличия проекта.

Реализационно (для backend): в цикле по `_ARGS_BY_TOOL` условие пропуска меняется с

```
if not include_server_side and name in SERVER_SIDE_TOOLS: continue
```

на эквивалент «пропускать только project-scoped site.* без проекта»; global server-side tools под фильтр не попадают. Сигнатуру можно оставить (`include_server_side` = «есть проект» = гейт `site.*`); смысловое уточнение — флаг относится к project-scoped, а не ко всем server-side. Семантика для `site.*` не меняется (обратная совместимость [ADR-022](ADR-022-optional-project-and-tool-gating.md)).

**Взаимодействие с осью B (assistant_mode, [Q-012-1](../99-open-questions.md), Open).** `time.now` — utility-инструмент, полезный в обоих режимах (chat и code). Нормативно: `time.now` **всегда** в offer-set обоих режимов (не подпадает под исключение `chat`-режимом, в отличие от `site.*`/`files.*`). Когда ось B будет реализована, `time.now` остаётся вне её фильтра.

### 4. Маршрутизация исполнения в orchestrator — до project-scoped ветки

В `_handle_tool_use` (`orchestrator.py`) для каждого `tool_use`-блока перед текущей веткой `if tool_name in SERVER_SIDE_TOOLS` добавляется **более ранняя** ветка для global server-side tools:

```
if tool_name in GLOBAL_SERVER_SIDE_TOOLS:
    # global: исполнить НЕМЕДЛЕННО, без external_project_id, без has_project-guard
    await self._execute_global_server_side_tool(...)
elif tool_name in SERVER_SIDE_TOOLS:
    assert external_project_id is not None   # project-scoped (ADR-011/022) — без изменений
    await self._execute_server_side_tool(..., external_project_id=external_project_id)
else:
    client_outs.append(...)   # client-side — hand-off к iOS
```

**Нормативные требования к маршрутизации (для backend):**
- Global-ветка **не вызывает** `_external_project_id()` и **не** опирается на `has_project`. `time.now` исполняется одинаково и при `project_id IS NULL`, и при непустом проекте.
- `assert external_project_id is not None` ([ADR-022](ADR-022-optional-project-and-tool-gating.md) §guard) применяется **только** к project-scoped `SERVER_SIDE_TOOLS`; global server-side tools проходят мимо него.
- Defensive-guard «`site.*` при `project_id IS NULL` → upstream-аномалия» сохраняется для project-scoped tools. Для `time.now` аномалии «нет проекта» не существует — он global по построению.
- Как и `site.*` ([ADR-011](ADR-011-server-side-tools.md)): global server-side tool исполняется в tool-loop, его `tool_result` персистится на бэке (`role="tool"`-шаг с `providerToolUseId`), в `toolCalls[]` наружу **НЕ** попадает; loop продолжается к Anthropic. Барьер хода ([ADR-025](ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)) учитывает только client-side вызовы — global server-side, как и `site.*`, исполнены немедленно.

### 5. Executor — отдельный global tool-handler, не привязанный к WebsiteService/проекту

Рекомендация (нормативно для backend): создать **отдельный** handler-класс (например `GlobalToolHandlers` в `src/app/chat/global_tools.py`), **не зависящий** от `WebsiteService`/`SiteToolHandlers`/проекта. Он:
- возвращает ту же структуру `ToolExecution` (`ok`/`error`/`to_tool_result_payload`), что и `SiteToolHandlers` ([website/tools.py](../../src/app/website/tools.py)) — единый контракт tool-result для orchestrator;
- получает время через инъектируемый **Clock-провайдер** (§ ADR + [06-testing-strategy.md](../06-testing-strategy.md)), а не прямой `datetime.now()` — для детерминизма qa;
- регистрируется в `_Deps` orchestrator рядом с `site_tools` (новое поле, например `global_tools`).

Обоснование: не загрязнять project-scoped `SiteToolHandlers` (его конструктор требует `WebsiteService` и завязан на проект); global tools должны быть исполнимы без website-инфраструктуры.

### 6. Контракт `time.now` (полностью — см. [chat-orchestrator/02-api-contracts.md](../modules/chat-orchestrator/02-api-contracts.md#timenow--server-side-global-tool-adr-026))

**Args (`TimeNowArgs`, Pydantic `_StrictModel`, `extra="forbid"`):**
```
{ "tz": "string (optional, IANA, напр. Europe/Moscow)" }
```
- `tz` — опциональный (`str | None = None`); IANA-имя зоны. Разумный лимит длины (нормативно `≤ 64` символа — длиннее любого валидного IANA-имени; вне лимита → tool-result error, не 422 хода). При отсутствии → UTC.
- `extra="forbid"`: любой другой ключ → ошибка валидации args (как у прочих tools).

**Result (UTC всегда + локальное при валидном `tz`):**
```json
{
  "utc": "2026-06-10T14:23:05.123456+00:00",
  "unix": 1781446985,
  "weekday": "Wednesday",
  "timezone": "Europe/Moscow",
  "local": "2026-06-10T17:23:05.123456+03:00"
}
```
- `utc` — всегда: текущее UTC-время, ISO8601 (RFC3339) с offset `+00:00`.
- `unix` — всегда: целочисленный Unix timestamp (секунды, UTC).
- `weekday` — всегда: английское имя дня недели по UTC-дате (`Monday`..`Sunday`).
- При **заданном и валидном** `tz`: дополнительно `timezone` (= нормализованное IANA-имя) и `local` (ISO8601 с локальным offset). Без `tz` → поля `timezone`/`local` **опущены** (результат = только UTC-набор).

**Невалидный/неизвестный `tz`** (превышение лимита длины **или** не парсится `zoneinfo`) → **tool-result error** (`ToolExecution.error(code="invalid_timezone", message=...)`), а **НЕ** падение хода (не 422, не 502). Модель получает машиночитаемую ошибку и может повторить без `tz` или с корректной зоной. Ход продолжается. Реализация резолва зоны ловит `(ZoneInfoNotFoundError, ValueError, OSError)`: `ZoneInfoNotFoundError`/`ValueError` — неизвестное/непарсимое IANA-имя; `OSError` — filesystem-hostile имя при **наличии** tz-базы (`ZoneInfo` трактует имя как путь, ОС отвергает его, напр. `OSError Errno 22`). Все три → `invalid_timezone`; UTC-набор всё равно отдаётся.

**Не мутирующий**: `time.now` не входит в `MUTATING_TOOLS` → нет `tool_mutation` audit-события (как `site.read`/`site.list`). Стандартные `tool_call_initiated`/`tool_call_completed` audit-события — как у любого tool (поведение `_execute_*` сохраняется).

### 7. Системный промт — generic-инструкция (EN, статична, оба режима)

В оба базовых промта (`_SYSTEM_PROMPT_CHAT` и `_SYSTEM_PROMPT_CODE`) добавляется **одинаковая статичная** строка-инструкция (дата НЕ вписывается):

> `You do not have built-in knowledge of the current date or time. If the user's request depends on the current date, time, or day of the week, call the time.now tool to get it; do not guess.`

**Инвариант prompt caching ([anthropic_client.py](../../src/app/chat/anthropic_client.py) `_build_system`, `cache_control: {"type":"ephemeral"}`):** инструкция **статична** (не содержит даты/времени и любых меняющихся значений), поэтому системный промт остаётся стабильным между запросами → **prompt cache не инвалидируется**. Это и есть причина выбора tool-подхода вместо инъекции даты в промт: инъекция даты ломала бы кэш на каждый запрос. Дата приходит **только** в tool-result, который не входит в кэшируемый системный префикс.

### 8. Clock-провайдер (инъектируемый источник времени)

Сейчас в коде нет инъектируемого источника времени (везде прямой `datetime.datetime.now(tz=datetime.UTC)`). Для детерминизма qa-тестов `time.now` обязан брать время через инъектируемый провайдер.

**Контракт (нормативно, для backend + qa):**
- `Protocol Clock` с методом `now() -> datetime.datetime` (timezone-aware, UTC). Дефолтная реализация `SystemClock.now()` = `datetime.datetime.now(tz=datetime.UTC)`.
- `GlobalToolHandlers` принимает `Clock` в конструкторе (дефолт `SystemClock()`); `time.now` вычисляет всё (`utc`/`unix`/`weekday`/`local`) от `clock.now()`.
- В тестах подаётся `FixedClock(fixed_dt)` → детерминированный результат (qa проверяет точный JSON-шейп при фиксированном времени и заданном `tz`).
- Форма инъекции — конструктор handler-класса (не глобальный FastAPI dependency), т.к. handler уже собирается в DI-графе orchestrator (`_Deps`); это минимальное изменение и не плодит FastAPI-зависимостей. Существующие прямые `datetime.now()` в других местах (например `site.preview` `expiresAt`) **этим ADR не трогаются** (вне scope; их рефактор на Clock — опционально, не требуется).

### 9. Биллинг — без изменений

Server-side раунд `time.now` исполняется внутри tool-loop одного message-шага и **НЕ** добавляет списаний: биллинг — 1 кредит = 1 сообщение, списание один раз на финальном `assistant_message` ([ADR-006](ADR-006-credit-billing-and-subscription-grant.md)), tool-раунды (client-side и server-side) не списывают. `time.now` ведёт себя как `site.*`-раунд: один или несколько вызовов в ходе → по-прежнему 1 кредит на сообщение. [ADR-006](ADR-006-credit-billing-and-subscription-grant.md) не меняется.

### 10. tzdata в prod-образе (зависимость зоны)

`zoneinfo` (stdlib) для локального времени по IANA-имени требует **базу таймзон**. На Linux `zoneinfo` ищет системную базу (`/usr/share/zoneinfo`); при её отсутствии — Python-пакет `tzdata` (если установлен). Базовый образ `python:3.12-slim-bookworm` ([Dockerfile](../../Dockerfile)) — **slim**, системная tz-база в нём может отсутствовать. **Пакет `tzdata` добавлен в зависимости проекта** (`pyproject.toml` — `tzdata>=2024.1`; `uv.lock` — `tzdata 2026.2`), поэтому в собранном prod-образе tz-база гарантирована: `ZoneInfo("Europe/Moscow")` резолвится, аргумент `tz` работает.

**Нормативно:** UTC-набор результата (`utc`/`unix`/`weekday`) **не** зависит от tz-базы (вычисляется от `datetime.UTC`) и работает всегда. Локальная часть (`tz` → `local`/`timezone`) требует tz-базы — она обеспечена pure-Python зависимостью `tzdata` (вариант A, без правки Dockerfile). Это закрывает [TD-019](../100-known-tech-debt.md) (**Resolved 2026-06-10**, scope devops). Невалидная/мусорная зона по-прежнему деградирует штатно (tool-result error `invalid_timezone`, ход не падает, UTC-набор отдаётся всегда) — см. §6.

## Последствия

**Плюсы:**
- Модель получает корректную дату/время по запросу, без хардкода и без инвалидизации prompt-кэша.
- `time.now` доступен в основном flow (чат-агрегатор без проекта) — закрывает исходный репорт.
- Чистое разделение: project-scoped (`site.*`) и global (`time.now`) server-side классы не смешиваются; `assert external_project_id is not None` остаётся корректным инвариантом для `site.*`.
- Read-only, не мутирующий, без новых списаний и без миграции БД.
- Clock-провайдер делает поведение детерминированно-тестируемым.

**Минусы / следствия:**
- Появляется второй реестр server-side tools (`GLOBAL_SERVER_SIDE_TOOLS`) и вторая ветка маршрутизации в `_handle_tool_use` — рост связности orchestrator (контролируемый, симметричный существующей `site.*`-ветке).
- Локальное время по `tz` зависит от tz-базы в образе ([TD-019](../100-known-tech-debt.md), Resolved 2026-06-10 — `tzdata` в зависимостях проекта); `tz` в prod работает. UTC-набор всегда доступен независимо от tz-базы.
- Каталог `/v1/tools` теперь содержит 14 tools (был 13); поле `execution` для `time.now` = `server`.

## Альтернативы (отклонены)

- **Хардкод даты в системный промт.** Отклонено решением пользователя: ломает prompt caching (промт меняется каждый запрос), и дата «протухает» в длинной сессии (кэш/контекст). Tool-подход даёт свежее время на момент вызова.
- **Расширить project-scoped `SERVER_SIDE_TOOLS` инструментом `time.now`.** Отклонено: `time.now` тогда предлагался бы только при наличии проекта ([ADR-022](ADR-022-optional-project-and-tool-gating.md)) и упёрся бы в `assert external_project_id is not None` — не работал бы в основном flow «чистого чата». Нужен именно отдельный global-класс.
- **client-side `time.now` (iOS отдаёт время).** Отклонено: лишний round-trip, зависимость от часов устройства (рассинхрон/подмена), и не работает для server-инициированных сценариев. Backend как источник времени надёжнее и без round-trip.
- **Инъекция даты как user/assistant-сообщения в контекст.** Отклонено: засоряет историю, тарифицируется в токенах каждый ход, и так же угадывается моделью при отсутствии. Tool вызывается только когда дата реально нужна.

## Задачи для backend (next steps)

- **tools.py:** добавить `TOOL_TIME_NOW = "time.now"`; новый frozenset `GLOBAL_SERVER_SIDE_TOOLS = {TOOL_TIME_NOW}` (не пересекается с `SERVER_SIDE_TOOLS`); внести `time.now` в `ALL_TOOL_NAMES`, `_DOMAIN_TO_ANTHROPIC`/`_ANTHROPIC_TO_DOMAIN` (`time_now`), `_ARGS_BY_TOOL` (`TimeNowArgs`), `TOOL_DESCRIPTIONS`. **НЕ** в `MUTATING_TOOLS`. Обновить `tool_catalog()`/`execution`-вычисление и `anthropic_tool_definitions()` (§3): `include_server_side` гейтит только `SERVER_SIDE_TOOLS`; `GLOBAL_SERVER_SIDE_TOOLS` предлагаются всегда.
- **global_tools.py (новый):** `Clock` Protocol + `SystemClock`; `GlobalToolHandlers(clock=SystemClock())` с методом `execute(tool_name, args) -> ToolExecution`, реализующим `time.now` по §6 (UTC всегда; `tz` валидный → `local`/`timezone`; невалидный → `ToolExecution.error("invalid_timezone", ...)`; лимит длины `tz` ≤ 64).
- **orchestrator.py:** в `_Deps` добавить `global_tools`; в `_handle_tool_use` добавить ветку `if tool_name in GLOBAL_SERVER_SIDE_TOOLS` **до** ветки `SERVER_SIDE_TOOLS` (исполнять без `external_project_id`, не трогать `has_project`-guard); метод `_execute_global_server_side_tool(...)` по образцу `_execute_server_side_tool` (персист tool-шага с `providerToolUseId`, `tool_call_completed` audit, без mutation-audit). В оба системных промта добавить статичную time.now-инструкцию (§7).
- **DI/wiring:** собрать `GlobalToolHandlers` там же, где `SiteToolHandlers`, передать в orchestrator.
- **Без миграции БД.** Без изменений в биллинге/policy/auth.

## Задачи для devops (next steps)

- **[TD-019](../100-known-tech-debt.md) — Resolved (2026-06-10):** tz-база обеспечена в runtime-образе добавлением pure-Python зависимости `tzdata` в проект (`pyproject.toml` — `tzdata>=2024.1`; `uv.lock` — `tzdata 2026.2`; вариант A, без правки Dockerfile). Аргумент `tz` работает в prod; UTC-набор работал и без этого.

## Задачи для qa (next steps)

- Контракт Clock (§8): тесты `time.now` подают `FixedClock` → детерминированный JSON-шейп. См. [06-testing-strategy.md §time.now](../06-testing-strategy.md).
