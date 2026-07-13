# ADR-063 — Удаление client-side инструментов `calendar.*` / `reminders.*` / `files.*` из сервиса

- **Статус:** Accepted
- **Дата:** 2026-07-14
- **Связано:** [ADR-011](ADR-011-server-side-tools.md) (классы tools: client-side vs server-side), [ADR-019](ADR-019-tools-catalog-endpoint.md) (каталог `GET /v1/tools`), [ADR-025](ADR-025-parallel-tool-calls-and-max-tokens-truncation.md) (протокол client-side `toolCalls[]` + барьер хода), [ADR-056](ADR-056-temporary-chat.md) (`include_client_side` gate), [ADR-024](ADR-024-history-payload-domain-normalization.md) (нормализация payload истории)
- **Супершедит:** [ADR-027](ADR-027-calendar-read-contract-alignment.md) (контракт `calendar.read` — инструмент удаляется целиком, унифицировать больше нечего)
- **Затрагивает (docs):** `docs/modules/chat-orchestrator/02-api-contracts.md`, `docs/modules/chat-orchestrator/03-architecture.md`, `docs/modules/chat-orchestrator/09-testing.md`, `docs/modules/chat-orchestrator/README.md`, `docs/modules/audit/02-api-contracts.md`, `docs/modules/audit/03-architecture.md`, `docs/modules/audit/09-testing.md`, `docs/modules/chats/02-api-contracts.md`, `docs/API-REFERENCE.md`, `docs/08-api-documentation.md`, `docs/01-architecture.md`, `docs/02-tech-stack.md`, `docs/figma-gap-analysis.md`, `docs/09-e2e-testing.md`, `docs/99-open-questions.md`
- **Затрагивает (код backend, ТЗ ниже):** `src/app/chat/tools.py`, `src/app/chat/orchestrator.py`, `src/app/api_gateway/routers/chat.py`, `src/app/schemas/chat.py`, `src/app/schemas/tools.py`, `src/app/config.py`
- **Характер изменения:** **удаление публичной поверхности** — 8 client-side инструментов исчезают из `GET /v1/tools`, модель больше не может их вызывать, iOS перестаёт их реализовывать. Не breaking для оставшегося контракта (эндпоинты, схемы survivors, протокол client-side — без изменений).

## Context

Сервис исторически предлагал модели три класса инструментов ([ADR-011](ADR-011-server-side-tools.md), [ADR-026](ADR-026-global-server-side-tools-and-time-now.md)):

- **client-side** (`files.read`, `files.write`, `files.list`, `files.mkdir`, `calendar.read`, `calendar.create_events`, `reminders.read`, `reminders.create`) — backend только **инициирует** tool-call (`status=tool_call`), исполняет **iOS-клиент** локально, результат приходит через `POST /v1/chat/tool-result`;
- **server-side project-scoped** (`site.*`, website-builder) — исполняет backend в tool-loop;
- **server-side global** (`time.now`, `quiz.generate`, `image.generate`) — исполняет backend без проекта.

**Решение владельца сервиса:** три семейства client-side инструментов (календарь, напоминания, файлы) — **полностью убрать из сервиса**. Модель не должна их предлагать/вызывать, `GET /v1/tools` не должен их перечислять, iOS-клиент прекращает их реализацию. Причина — продуктовая (владелец сузил функциональность до основного flow: чат-агрегатор + website-builder + генерация изображений/квизов), не техническая.

На момент решения (подтверждено чтением `src/app/chat/tools.py`) каталог `_ARGS_BY_TOOL` содержит **16** инструментов: 8 удаляемых client-side + 5 `site.*` + 3 global (`time.now`, `quiz.generate`, `image.generate`). После удаления остаётся **8** инструментов — **все server-side**.

Важное следствие, зафиксированное в §3: **после удаления в классе client-side не остаётся ни одного зарегистрированного инструмента**. Это делает решение о судьбе client-side **протокола** (эндпоинт `/chat/tool-result`, поле `toolCalls[]`, барьер хода [ADR-025](ADR-025-parallel-tool-calls-and-max-tokens-truncation.md), gate `include_client_side` [ADR-056](ADR-056-temporary-chat.md)) — отдельным вопросом (§3, [Q-063-1](../99-open-questions.md)).

## Decision

### 1. Удаляемые инструменты (нормативно)

Полностью удаляются **8** client-side инструментов:

| Домен-имя | Backend-константа | Args-модель |
|---|---|---|
| `files.read` | `TOOL_FILES_READ` | `FilesReadArgs` |
| `files.write` | `TOOL_FILES_WRITE` | `FilesWriteArgs` |
| `files.list` | `TOOL_FILES_LIST` | `FilesListArgs` |
| `files.mkdir` | `TOOL_FILES_MKDIR` | `FilesMkdirArgs` |
| `calendar.read` | `TOOL_CALENDAR_READ` | `CalendarReadArgs` |
| `calendar.create_events` | `TOOL_CALENDAR_CREATE` | `CalendarCreateArgs` (+ `CalendarEventInput`) |
| `reminders.read` | `TOOL_REMINDERS_READ` | `RemindersReadArgs` |
| `reminders.create` | `TOOL_REMINDERS_CREATE` | `RemindersCreateArgs` (+ `ReminderInput`) |

После удаления модель их не предлагает (нет в `_ARGS_BY_TOOL` → нет в `anthropic_tool_definitions`/`neutral_tool_definitions`/`openai_tool_definitions` → нет в промте tools), `GET /v1/tools` их не перечисляет, `to_anthropic_tool_name`/`to_domain_tool_name` их не знают.

### 2. Точки удаления в `src/app/chat/tools.py` (нормативно)

Удаляются:
- 8 констант имён `TOOL_FILES_READ|WRITE|LIST|MKDIR`, `TOOL_CALENDAR_READ|CREATE`, `TOOL_REMINDERS_READ|CREATE`;
- их 8 членств в множестве `ALL_TOOL_NAMES`;
- их 8 записей в статической карте `_DOMAIN_TO_ANTHROPIC` (обратная `_ANTHROPIC_TO_DOMAIN` строится из неё — автоматически схлопывается);
- 4 членства мутирующих в `MUTATING_TOOLS` (`TOOL_FILES_WRITE`, `TOOL_FILES_MKDIR`, `TOOL_CALENDAR_CREATE`, `TOOL_REMINDERS_CREATE`);
- args-классы: `FilesReadArgs`, `FilesWriteArgs`, `FilesListArgs`, `FilesMkdirArgs`, `CalendarReadArgs`, `CalendarEventInput`, `CalendarCreateArgs`, `RemindersReadArgs`, `ReminderInput`, `RemindersCreateArgs`;
- их 8 записей в `_ARGS_BY_TOOL`;
- их 8 записей в `TOOL_DESCRIPTIONS`;
- упоминания удалённых семейств в docstring модуля и в docstring `neutral_tool_definitions` (`files.*/calendar.*/reminders.*`).

**Проверка литералов (Verify-before-assert).** Множества `CLIENT_SIDE_TOOLS` и `TOOL_METRIC_LABELS` в коде **не существуют** — членство в классе client-side вычисляется функцией `_is_client_side(name)` (= «не в `SERVER_SIDE_TOOLS` и не в `GLOBAL_SERVER_SIDE_TOOLS`»), метрик-лейблов-констант нет. Backend НЕ должен искать/править эти несуществующие символы.

Обновляется (не удаляется): комментарий у `_DOMAIN_TO_ANTHROPIC` — счётчик «13 fixed pairs» → фактическое число оставшихся пар (**8**: 5 `site_*` + `time_now` + `quiz_generate` + `image_generate`). Backend пересчитывает по факту, не берёт число из этого ADR вслепую.

### 2a. OpenAPI-примеры и Field-описания (нормативно)

Три места рекламируют удалённые инструменты через примеры и попадают в OpenAPI-схему (`/docs`, `/openapi.json`) — их надо очистить:

- **`src/app/api_gateway/routers/chat.py`** — пример ответа `status=tool_call` использует `name: "files.write"` в `toolCalls[]` и в deprecated-поле `toolCall` (несколько вхождений). **Решение:** заменить имя инструмента в этом примере на явно-гипотетический плейсхолдер `example.client_tool` (протокол `tool_call`/`toolCalls[]` сохраняется спящим, но реального client-side инструмента для примера нет). Сам пример-статус `tool_call` НЕ удалять — он иллюстрирует форму поля. Одну короткую человекочитаемую пометку, что имя иллюстративное (client-side инструментов сейчас в поставке нет), допустимо добавить в `description` примера/операции **без** маркеров `ADR-`/`§`.
- **`src/app/schemas/chat.py`** `ToolCallSchema.name` `Field(description=...)` — пример «например, `files.read`» → «например, `example.client_tool`» (это имя client-side инструмента для исполнения на устройстве; реальных сейчас нет — плейсхолдер иллюстративный).
- **`src/app/schemas/tools.py`** `ToolDescriptor.name` `Field(description=...)` — пример «например `files.read`» → «например `site.write_file`» (это дескриптор каталога `GET /v1/tools`, где сейчас только server-side инструменты — пример должен быть реальной записью каталога).

**Требование к строкам `description`:** НЕ вставлять в них ссылки `ADR-`/`Q-`/`TD-`/`§` — они уходят в публичный OpenAPI (правило Swagger-чистоты, `08-api-documentation.md`).

### 3. Что НЕ трогается (нормативно)

- **Server-side инструменты — без изменений:** `time.now` (`TOOL_TIME_NOW`), `quiz.generate` (`TOOL_QUIZ_GENERATE`), `image.generate` (`TOOL_IMAGE_GENERATE`), `site.write_file`/`site.preview`/`site.list`/`site.read`/`site.delete` (`SERVER_SIDE_TOOLS`) — их константы, args-модели, `TOOL_DESCRIPTIONS`, членство в `MUTATING_TOOLS` (`site.write_file`/`site.delete`/`image.generate`), `_DOMAIN_TO_ANTHROPIC`, gating-логика, dispatch — остаются как есть. Инварианты [ADR-011](ADR-011-server-side-tools.md) (server-side исполнение) не затрагиваются: удаляются только client-side семейства-примеры, но **сам класс** server-side и его контракт неизменны.
- **Client-side протокол-машинерия — сохраняется, но становится «спящей».** Эндпоинт `POST /v1/chat/tool-result`, поле `toolCalls[]`/`toolCall` в `ChatResponse`, барьер хода ([ADR-025](ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)), функция `_is_client_side` и параметр `include_client_side` ([ADR-056](ADR-056-temporary-chat.md)) **остаются в коде и контракте без изменений**. После удаления в классе client-side нет зарегистрированных инструментов, поэтому в штатном потоке `status=tool_call`/`toolCalls[]` больше не возникает — но инфраструктура сохранена для (а) обратной совместимости публичного контракта, (б) реплея старых сессий, (в) будущих client-side инструментов. Удалять протокол в этом ADR **не** решено — см. [Q-063-1](../99-open-questions.md) (дефолт: сохранить). Это минимально-достаточное изменение: убираем инструменты, не реформируем протокол.
- **`config.py`:** ключей/env этих инструментов нет; единственная ссылка — поясняющий комментарий у `ANTHROPIC_MAX_TOKENS` («several files.write with full content»). Он **не** ломает поведение; backend обновляет формулировку на актуальный пример генерации файлов (`site.write_file`), т.к. `files.write` больше не существует.

### 4. Каталог `GET /v1/tools` ([ADR-019](ADR-019-tools-catalog-endpoint.md))

Каталог генерируется из `_ARGS_BY_TOOL` (single source of truth) — сокращается автоматически с **16** до **8** инструментов. Контракт эндпоинта (поля `name`/`description`/`mutating`/`execution`/`inputSchema`, JWT-защита, детерминированный порядок) — **без изменений**. Оставшиеся 8:

| name | execution | mutating |
|---|---|---|
| `site.write_file` | server | да |
| `site.preview` | server | нет |
| `site.list` | server | нет |
| `site.read` | server | нет |
| `site.delete` | server | да |
| `time.now` | server (global) | нет |
| `quiz.generate` | server (global) | нет |
| `image.generate` | server (global) | да |

Примечание: до этого ADR нормативные docs расходились в счётчике (`chat-orchestrator/02` — «16», `API-REFERENCE` — «14»); этот ADR выравнивает обе цифры на фактические **8**.

### 5. Обратная совместимость и данные — миграция НЕ требуется (нормативно)

Удаляются **дефиниции** инструментов, а не исторические данные. Старые строки `tool_calls` (`tool_name='files.write'` и т.п.) и `chat_steps.payload` с этими именами **остаются в БД и продолжают отдаваться**; новые такие строки не порождаются. Backfill/миграция бессмысленны и **не вводятся**.

Инварианты, подтверждённые чтением кода:
- **Чтение истории `GET /v1/chats/{id}`** (`ChatsService._normalize_payload`): при нормализации `tool_use.name` вызывает `to_domain_tool_name`, который на удалённом имени бросит `UnknownToolNameError`; хендлер уже **ловит** это исключение (`except UnknownToolNameError`) и оставляет имя как есть (лог-warning). Эндпоинт не падает; деградация чисто косметическая (старый шаг покажет underscore-имя `files_write` вместо dot). Это **существующее** поведение, менять не нужно.
- **`GET /v1/chats/{id}/steps`** резолвит `tool_name` из строки `tool_calls` (доменное имя хранится в БД), не через реестр — старые шаги отображаются корректно.
- **Реплей/continuation старых сессий** (`_build_messages` → провайдер-клиент): сохранённый content ассистента реплеится **дословно** в wire-форме провайдера (Anthropic: underscore-имена уже в payload; OpenAI аналогично). `to_anthropic_tool_name` применяется **только** к tool-**дефинициям** (`anthropic_tool_definitions`/`_serialize_tools`), которые удалённые инструменты больше не содержат, — поэтому реплей исторических `tool_use`-блоков удалённых инструментов **не** проходит через маппинг и **не** бросает `UnknownToolNameError`.
- **Аудит:** старые `tool_mutation` записи для удалённых инструментов остаются; `MUTATING_TOOLS` теряет 4 членов — новые мутации для удалённых инструментов не возникают.

**Принятый редкий риск (не блокирует):** сессия с **открытым** (незакрытым) client-side tool-loop по удалённому инструменту, переживающая деплой, при попытке continuation может завершиться контролируемой ошибкой (`validate_tool_args`/барьер для несуществующего инструмента). Без порчи данных; пользователь повторяет ход. Вероятность мала (окно = ход, физически исполняемый на устройстве в момент деплоя), специальной обработки не вводим.

### 6. Судьба [ADR-027](ADR-027-calendar-read-contract-alignment.md) и связанных open questions

- **[ADR-027](ADR-027-calendar-read-contract-alignment.md)** (унификация контракта `calendar.read`) — **Superseded этим ADR**: инструмент `calendar.read` удаляется целиком, унифицировать нечего. Тело ADR-027 сохраняется (immutable), в заголовок добавляется supersede-пометка и blockquote-предупреждение со ссылкой на ADR-063.
- **[Q-027-1](../99-open-questions.md)** (tz-aware vs naive local для календаря) — **Closed (moot)**: календарные инструменты удалены, вопрос формата их аргументов неактуален. **[Q-027-2](../99-open-questions.md)** уже Closed — без изменений (историческая констатация «миграции нет» остаётся верной).
- **Тест `tests/unit/test_calendar_read_contract_adr027.py`** — контракт-тест удаляемого инструмента; **удаляется** (зона qa; здесь фиксируется намерение). Прочие затронутые тесты — см. ТЗ / handoff qa.

### 7. Системный промт модели ([ADR-026](ADR-026-global-server-side-tools-and-time-now.md) — статичность промта сохраняется)

В `src/app/chat/orchestrator.py` системные промты (`_SYSTEM_PROMPT_CHAT`, `_SYSTEM_PROMPT_CODE`) перечисляют «tools that the user's device executes locally (files, calendar, reminders)». Эти упоминания удаляются (перечисленные локальные инструменты больше не существуют). Прочая структура промтов, инъекция `_TIME_NOW_INSTRUCTION`, prompt-cache-устойчивость (промт остаётся статичным) — не затрагиваются. Формулировку финальной строки backend подбирает в рамках этого ADR (например убрать перечисление локальных инструментов, оставив упоминание server-side site-tools в code-режиме).

### 8. Публичный iOS-контракт

iOS-клиент перестаёт реализовывать удалённые инструменты. Поскольку модель их больше не вызывает, `status=tool_call` с этими именами не приходит — старый iOS-код исполнения этих tools становится мёртвым, но не вызывает ошибок (не активируется). Скоординированный релиз iOS **не требуется для корректности backend** (в отличие от breaking-переименования [ADR-027](ADR-027-calendar-read-contract-alignment.md)); iOS убирает соответствующий UI/пермишены в своём темпе.

## Consequences

**Положительные:**
- Сужение публичной tool-поверхности до фактически поддерживаемой (8 server-side инструментов); каталог `GET /v1/tools` перестаёт обещать неподдерживаемую функциональность.
- Меньше кода/схем/описаний в `tools.py`; single-source-of-truth (`_ARGS_BY_TOOL`) автоматически чинит каталог и промт.
- Устранён давний рассинхрон счётчика инструментов в docs (16 vs 14 → 8).
- Токен-экономия: модель не получает описания 8 неиспользуемых инструментов в каждом запросе.

**Отрицательные / риски:**
- Пользователи теряют функциональность календаря/напоминаний/файлов на устройстве (осознанное продуктовое решение владельца).
- В классе client-side не остаётся инструментов → протокол `/chat/tool-result`/`toolCalls[]`/барьер становится «спящим». Тесты, использовавшие `files.*`/`calendar.*` как образцовый client-side инструмент для проверки протокола (барьер, parallel tool use [ADR-025](ADR-025-parallel-tool-calls-and-max-tokens-truncation.md), client-side gate [ADR-056](ADR-056-temporary-chat.md)), должны переключиться на **тестовый фейковый** client-side инструмент (регистрируется фикстурой/monkeypatch в тестах, не поставляется), а не на реальный. Зона qa. См. [Q-063-1](../99-open-questions.md).
- Косметическая деградация отображения очень старых сессий (underscore-имя удалённого инструмента в истории) — приемлемо, эндпоинт не падает (§5).

## Alternatives

1. **Оставить инструменты, скрыть только из каталога/промта (флагом).** Отклонено: владелец потребовал **полное удаление из сервиса**, а не скрытие; полускрытые дефиниции — источник рассинхрона и мёртвого кода.
2. **Удалить и client-side протокол целиком** (`/chat/tool-result`, `toolCalls[]`, барьер, `include_client_side`). Отклонено в этом ADR: гораздо более широкое breaking-изменение, затрагивает [ADR-025](ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)/[ADR-056](ADR-056-temporary-chat.md) и публичный контракт; владелец просил убрать инструменты, не протокол. Вынесено как [Q-063-1](../99-open-questions.md) (дефолт: сохранить протокол «спящим»).
3. **Мигрировать/чистить исторические `tool_calls`/`chat_steps` с удалёнными именами.** Отклонено: исторические данные валидны для своих ходов, чтение уже деградирует безопасно (§5); миграция — риск без выгоды.
4. **Пометить ADR-011/ADR-019 как superseded.** Отклонено: их **нормативное** содержание (класс server-side; контракт каталога) не отменяется — удаляются лишь client-side семейства-примеры. Тело этих ADR остаётся валидным; ADR-063 фиксирует, что перечисления инструментов в их прозе стали историческим срезом.
