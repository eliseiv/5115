# ADR-038 — Перенос существующего чата в воркспейс (изменяемый `workspaceProjectId` + workspace-инъекция на каждом ходе)

- Статус: Accepted
- Дата: 2026-06-18
- Расширяет / уточняет: [ADR-036](ADR-036-workspaces-implementation.md) (Workspaces — реализация). Делает `chat_sessions.workspace_project_id` **явно изменяемым** через новый эндпоинт и меняет условие инъекции workspace-контекста в orchestrator.
- Связан с: [ADR-013](ADR-013-workspace-projects-vs-website-builder.md) (концепция workspace ≠ website-builder), [ADR-012](ADR-012-assistant-mode-vs-billing-mode.md) (base assistant_mode prompt), [ADR-033](ADR-033-llm-provider-abstraction.md) (провайдер-агностичная подача), [ADR-006](ADR-006-credit-billing-and-subscription-grant.md) (биллинг неизменен), [ADR-020](ADR-020-inline-base64-attachments-mvp.md)/[ADR-024](ADR-024-history-payload-domain-normalization.md) (история/реплей).
- Без миграции БД (колонка `chat_sessions.workspace_project_id` уже добавлена миграцией `0011`, [ADR-036 §2](ADR-036-workspaces-implementation.md)).

## Context

Сценарий iOS (репорт пользователя): пользователь создал **обычный чат** (`workspace_project_id IS NULL`), затем хочет **перенести его в проект** (воркспейс), чтобы инструкции и файлы-знания проекта применялись к этому чату дальше.

Текущее состояние кода (разведка 2026-06-18, факты — docs↔код согласованы):
- `chat_sessions.workspace_project_id` (uuid, nullable, FK `workspace_projects.id` ON DELETE SET NULL) — есть (`0011`, [ADR-036 §2](ADR-036-workspaces-implementation.md)).
- Привязка **session-fixed**: пишется только при создании сессии в `POST /v1/chat/run` (валидация принадлежности user→`404`, [chat-orchestrator/02-api-contracts.md `workspaceProjectId`](../modules/chat-orchestrator/02-api-contracts.md#workspaceprojectid-adr-036)); на resume поле запроса игнорируется. **Изменить привязку у существующей сессии нечем** — это и добавляется.
- `ChatListItemSchema.workspaceProjectId` — реальное значение из сессии; фильтр `GET /v1/chats?workspaceProjectId=` есть ([chats/02-api-contracts.md](../modules/chats/02-api-contracts.md#get-v1chats)).
- **Инъекция workspace-контекста в orchestrator** (`src/app/chat/orchestrator.py`, проверено построчно):
  - `instructions` → `system`-prompt: на **turn 0** (`ctx.is_new and sess.workspace_project_id is not None`, строка ~497, через `context_for_session`) **И** на **continuation** `/chat/tool-result` (строка ~672, `if sess.workspace_project_id is not None:` → `instructions_for_session`). Continuation **не** завязан на `is_new`.
  - `files` (knowledge) → как контекст user-turn **только на turn 0** (`ctx.is_new`, строка ~497, `ws_context.attachments` → `_merge_attachments`). На continuation файлы не подаются (они уже в истории как content-блоки).
- PATCH `/v1/chats/{id}` сейчас умеет только `title`/`isPinned` (`ChatPatchRequest`, [chats/02-api-contracts.md](../modules/chats/02-api-contracts.md#patch-v1chatsid)); метаданные правит `ChatsRepository.update_metadata` (только `title`/`is_pinned`/`updated_at`).

**Проблема дизайна (ключевая).** Инъекция **файлов** завязана на `ctx.is_new` (turn 0). У **перенесённого** чата следующие сообщения — **не turn 0** → по текущей логике ни instructions (для нового workspace), ни файлы не применятся автоматически на ближайшем ходе через turn-0-ветку. Смысл переноса — чтобы проект влиял на чат **дальше**. Нужно решить, что именно из workspace-контекста и как применяется к перенесённому чату.

## Decision

### 1. API-форма: единый `PATCH /v1/chats/{chat_id}` с полем `workspaceProjectId`

Расширить существующий `PATCH /v1/chats/{chat_id}` (тег `Chats`, JWT, изоляция по `sub`) полем `workspaceProjectId: uuid | null`. **Выделенный** `POST /v1/chats/{id}/workspace` отвергнут (см. Alternatives).

- Тело (любое непустое подмножество полей; правило «хотя бы одно поле» сохраняется и расширяется на `workspaceProjectId`):
  ```json
  { "title": "string?", "isPinned": "bool?", "workspaceProjectId": "uuid | null" }
  ```
- Семантика `workspaceProjectId` (различение «поле отсутствует» vs «поле = null» — по `model_fields_set`, как уже сделано для `title`):
  - **поле отсутствует** в теле → привязка чата **не трогается** (изменяются только переданные `title`/`isPinned`);
  - **`workspaceProjectId = <uuid>`** → перенести/сменить: установить `chat_sessions.workspace_project_id = uuid` (валидация — §2);
  - **`workspaceProjectId = null`** → убрать из воркспейса: `chat_sessions.workspace_project_id = NULL` (чат становится обычным).
- Эта единая форма покрывает все три операции: **перенести** в проект, **сменить** проект, **убрать** из проекта.
- Ответ `PATCH` расширяется полем `workspaceProjectId` (актуальное значение после изменения), симметрично остальным полям ответа.

### 2. Валидация и изоляция

- **Сессия принадлежит user** (`chat_sessions.user_id = sub`): чужой/несуществующий чат → **`404`** (как у всех `/v1/chats/*`, BR-CH-1, не раскрывать чужое существование). Проверка — уже существующий `ChatsService._require_session`.
- **Целевой workspace принадлежит user** (при `workspaceProjectId = <uuid>`): иначе → **`404 workspace_not_found`** — **консистентно с `/chat/run`** ([ADR-036 §3](ADR-036-workspaces-implementation.md): чужой/несуществующий workspace при создании сессии → `404 workspace_not_found`). Вариант `422` отвергнут ради симметрии семантики «несуществующий ресурс не раскрывается» (тот же код, что для тела `/chat/run`). Проверка переиспользует `WorkspacesService.owns_workspace(workspace_id, sub)` (уже есть, ADR-036).
- **`workspaceProjectId = null`** → снять привязку, без обращения к workspaces-сервису (валидировать нечего).
- **Идемпотентность:** повторный PATCH с тем же значением (тем же uuid или повторный null) → `200`, тот же результат, без ошибки. Установка значения, равного текущему, — допустима.
- **`extra='forbid'`** на теле сохраняется. Правило «хотя бы одно поле» (`title`/`isPinned`/`workspaceProjectId`) — присутствие любого из трёх в `model_fields_set`.
- Изоляция кросс-проверки: workspace-валидация выполняется **тем же** `sub`, что владеет чатом — нельзя привязать свой чат к чужому workspace (чужой → `404`, не раскрывается). Модель угроз — [05-security.md §Workspaces](../05-security.md).

### 3. Поведение orchestrator для перенесённого чата (КЛЮЧЕВОЕ, ТЗ для backend)

Смысл переноса — чтобы проект влиял на чат **со следующего сообщения**. Текущая инъекция привязана к turn 0, поэтому требуется явное изменение условия.

#### 3.1. `instructions` — применяются на КАЖДОМ ходе сессии с `workspace_project_id` (turn 0 И continuation)

- **Решение: ДА**, `instructions` живого (на момент хода) workspace инъектируются в `system`-prompt на **каждом** обращении к LLM для сессии с непустым `workspace_project_id` — **независимо от `is_new`**.
- **Изменение условия в `run()`** (`orchestrator.py` ~строка 497): turn-0-ветка сейчас — `if ctx.is_new and sess.workspace_project_id is not None:`. Условие **инъекции instructions** меняется на «workspace есть» (`sess.workspace_project_id is not None`), **развязывается** от `ctx.is_new`. То есть на любом `/chat/run` (новая сессия ИЛИ resume/следующее сообщение перенесённого чата) при наличии workspace `system`-prompt собирается через `_system_prompt_with_workspace(assistant_mode, instructions)`.
  - На **turn 0** instructions берутся из уже собираемого `context_for_session` (instructions + файлы) — без изменений.
  - На **resume/следующем сообщении** (`not ctx.is_new`) instructions читаются лёгким single-column чтением `WorkspacesService.instructions_for_session(workspace_id, sub)` (тот же helper, что уже используется на continuation, строка ~672–676) — **файлы при этом НЕ собираются** (см. §3.2).
- Это **консистентно** с уже принятым ADR-036 §3: instructions живут в параметре `system` (не в истории сообщений), поэтому переинъектируются на каждом обращении к LLM (turn 0, resume, continuation). Перенос лишь распространяет «resume»-инъекцию на сессии, у которых workspace появился позже.
- **Живой workspace на момент хода:** instructions читаются из текущего состояния `workspace_projects` (а не «снимка на момент переноса»). Удалён/опустошён workspace или пустые instructions → инъекции нет (`base` system-prompt, prompt-cache не ломается; graceful — как на continuation сейчас).
- Провайдер-агностично ([ADR-033](ADR-033-llm-provider-abstraction.md)) — это часть `system`, одинаково для Anthropic/OpenAI.

#### 3.2. `files` (knowledge) — НЕ переинъектируются ретроспективно (вариант a, минимально-сложный)

- **Решение: вариант (a)** — файлы-знания **НЕ** подаются автоматически на ходах перенесённого чата. Полная подача файлов (`extracted_text` как текст + images как vision) остаётся **turn-0-only**: файлы доступны как контекст только тем чатам, что **изначально созданы** в воркспейсе (turn 0 с workspace), где они становятся content-блоками истории и реплеятся автоматически.
- **Изменение условия:** ветка подачи **файлов** (`workspace_attachments = ws_context.attachments` через `context_for_session`) остаётся под `ctx.is_new and sess.workspace_project_id is not None` (turn 0). То есть после развязки instructions от `is_new` (§3.1) на **resume** (`not ctx.is_new`) собираются **только instructions** (`instructions_for_session`), а `context_for_session` (который тянет файлы) **не** вызывается. На **turn 0** поведение прежнее (instructions + файлы через `context_for_session`).
- **Обоснование (стоимость/контекст/кэш):**
  - **Стоимость токенов и контекст-окно:** `extracted_text` всех файлов ограничен `WORKSPACE_CONTEXT_MAX_CHARS` (дефолт 200000, [ADR-036 §6](ADR-036-workspaces-implementation.md)) — это крупный блок. Инъекция на каждом ходе перенесённого чата дублировала бы его в каждый запрос и в историю при каждом сообщении, раздувая стоимость и рискуя переполнением окна на длинных чатах.
  - **Prompt-cache:** файлы подаются в **user-content/историю** (не в `system`). Их повторная вставка на произвольном ходе меняла бы хвост истории нестабильно; instructions же живут в `system` и переинъекция там дешева/кэш-нейтральна (system от хода к ходу одинаков при неизменных instructions).
  - **Симметрия с inline-attachments:** chat-вложения ([ADR-020](ADR-020-inline-base64-attachments-mvp.md)) тоже принимаются только в первом message-шаге и не реинъектируются — файлы-знания workspace следуют той же модели «тяжёлый контент один раз».
  - **MVP-простота:** не требует нового механизма «отметить файлы как поданные» / дедупликации в истории.
- **Что это значит для пользователя (зафиксировать в UX-доке iOS вне backend-scope):** после переноса обычного чата в проект на этот чат начинают влиять **инструкции** проекта (со следующего сообщения), но **файлы-знания ретроспективно не подмешиваются**. Чтобы файлы участвовали с самого начала — чат следует **создавать** уже в воркспейсе. Ретроактивная подача файлов перенесённому чату — отложена как [Q-038-1](../99-open-questions.md) (вариант b — реинъекция файлов на первом ходе после переноса — возможное будущее усиление).

#### 3.3. Сводка изменения поведения orchestrator (ТЗ для backend)

| Контекст | До (ADR-036) | После (ADR-038) |
|---|---|---|
| `instructions`, turn 0 (новая сессия workspace) | инъекция (`context_for_session`) | без изменений |
| `instructions`, continuation `/chat/tool-result` | инъекция (`instructions_for_session`) | без изменений |
| `instructions`, **resume/следующий ход** (`/chat/run`, `not is_new`) сессии с workspace | **нет** (turn-0-only) | **инъекция** (`instructions_for_session`) — **новое** |
| `files`, turn 0 (новая сессия workspace) | инъекция (`context_for_session.attachments`) | без изменений |
| `files`, resume/continuation | нет | **нет** (без изменений) — вариант (a) |

### 4. Связь с session-fixed семантикой (непротиворечиво)

- Поле `workspaceProjectId` в `POST /v1/chat/run` остаётся **session-fixed**: фиксируется при создании сессии; на resume поле запроса игнорируется ([ADR-036 §3](ADR-036-workspaces-implementation.md), без изменений). Путь `/chat/run` **не** становится каналом смены привязки — это устранило бы single-source и создало бы двусмысленность «изменить через resume».
- Привязка становится **явно изменяемой ТОЛЬКО** через `PATCH /v1/chats/{id}` (этот ADR). Таким образом: `/chat/run` **устанавливает** привязку один раз при создании; `PATCH` — **управляет** ею после (перенести/сменить/убрать). Двух конкурирующих путей записи нет — `/chat/run` на resume по-прежнему её не трогает.
- Терминологически: «session-fixed для `/chat/run`» = «значение не берётся из тела resume-запроса генерации», а не «иммутабельно навсегда». Управление привязкой вынесено в управляющий эндпоинт чата, что согласуется с тем, что прочие управляющие операции над сессией (`title`/`isPinned`/`delete`) живут в модуле `chats`, а не в `/chat/run`.

### 5. Биллинг, миграция, аудит

- **Биллинг — не затрагивается** ([ADR-006](ADR-006-credit-billing-and-subscription-grant.md)): PATCH чата (включая смену привязки) — управляющая операция, ledger не пишется; генерация в чате — 1 кредит = 1 сообщение как обычно. Инъекция instructions на amount не влияет.
- **Миграции нет** — колонка `chat_sessions.workspace_project_id` уже существует (`0011`, [ADR-036 §2](ADR-036-workspaces-implementation.md)).
- **Аудит:** **новое аудит-событие не вводится.** Текущий `PATCH /v1/chats/{id}` (rename/pin) аудит-событие не пишет; смена привязки — операция того же класса (метаданные сессии), симметрия сохраняется. Существующий реестр `event_type` (`src/app/audit/service.py`) не расширяется. Если позже потребуется трассировка переносов — отдельным решением (не блокирует фичу).

## Consequences

- **Контракт (аддитивно для существующих клиентов):** `ChatPatchRequest` получает опциональное `workspaceProjectId: uuid | None`; `ChatPatchResponse` — `workspaceProjectId: uuid | None`. Старые клиенты, шлющие только `title`/`isPinned`, не затронуты (поле опционально, привязка не трогается при отсутствии поля). Различение absent vs `null` — через `model_fields_set` (как уже сделано для `title`).
- **Код backend** (ТЗ — §«Указания backend» ниже): расширение `ChatPatchRequest`/`ChatPatchResponse` (`src/app/schemas/chats.py`), валидатора «хотя бы одно поле»; `ChatsService.rename_or_pin` (или новый метод) принимает `set_workspace_project_id` + `workspace_project_id` и валидирует целевой workspace через инъецированный `WorkspacesService.owns_workspace`; `ChatsRepository.update_metadata` пишет новую колонку; роутер `patch_chat` пробрасывает поле и зависимость workspaces-сервиса. **Orchestrator** (`run()`): развязать инъекцию **instructions** от `ctx.is_new` (инъектировать при `sess.workspace_project_id is not None`), сохранив **files** под `ctx.is_new` (turn-0-only). `chats`-модуль теперь зависит от `workspaces`-сервиса (read-only `owns_workspace`) — допустимо (chats уже знает про `workspace_project_id`).
- **docs↔код:** на момент ADR расхождений нет; после реализации `docs/modules/chats/02-api-contracts.md`, `docs/modules/workspaces/*`, `docs/modules/chat-orchestrator/*`, `API-REFERENCE.md`, `05-security.md` обновлены этим проходом (architect) — backend приводит код в соответствие.
- **Угрозы:** перенос — изменение принадлежности контекста; изоляция по `sub` на обеих сторонах (чат и workspace), чужой ресурс → `404`. instructions/файлы остаются пользовательским контентом, подаваемым модели; модель угроз инъекции — [05-security.md §Workspaces](../05-security.md) (без исполнения, лимиты, redaction логов) — неизменна.
- **UX-следствие (iOS):** перенос распространяет на чат **инструкции** проекта (со следующего сообщения), но **не файлы-знания** ретроспективно (§3.2). Документируется как контракт; ретроактивная подача файлов — [Q-038-1](../99-open-questions.md).

## Alternatives

- **Выделенный `POST /v1/chats/{id}/workspace` (+ `DELETE` для снятия).** Отвергнуто: дублирует управляющую поверхность чата (rename/pin/delete уже на `PATCH`/`DELETE /v1/chats/{id}`); единый `PATCH` с `workspaceProjectId` атомарно покрывает перенести/сменить/убрать (`null`) одним полем, без второго эндпоинта и без отдельного метода удаления. iOS делает один вызов.
- **Смена привязки через `/chat/run` (снять session-fixed).** Отвергнуто: создало бы второй путь записи привязки и двусмысленность «resume меняет привязку», ломая single-source ([ADR-036 §3](ADR-036-workspaces-implementation.md)). Управление вынесено в управляющий эндпоинт.
- **Ретроактивная подача файлов-знаний перенесённому чату (вариант b).** Отвергнуто на старте: стоимость токенов/контекст-окна, нестабильность prompt-cache, асимметрия с inline-attachments, доп. механизм дедупликации. Отложено как [Q-038-1](../99-open-questions.md); чат, которому нужны файлы с начала, создаётся в воркспейсе.
- **`422` для несуществующего целевого workspace.** Отвергнуто: `/chat/run` уже отвечает `404 workspace_not_found` ([ADR-036 §3](ADR-036-workspaces-implementation.md)); единый код устраняет асимметрию и не раскрывает чужое существование.
- **Новое аудит-событие `chat_workspace_change`.** Отвергнуто на старте: текущий PATCH (rename/pin) аудит не пишет; перенос — операция того же класса. Можно ввести позже без слома контракта.
