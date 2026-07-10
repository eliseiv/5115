# ADR-022 — Опциональный `projectId` в `/v1/chat/run` + гейтинг `site.*` по наличию проекта

- Статус: Accepted
- Дата: 2026-06-04
- Связанные: [ADR-011](ADR-011-server-side-tools.md) (server-side `site.*`), [ADR-012](ADR-012-assistant-mode-vs-billing-mode.md) (атрибут сессии, фиксируется при создании), [ADR-013](ADR-013-workspace-projects-vs-website-builder.md) (project ≠ workspace), [ADR-010](ADR-010-backend-hosted-preview.md) (preview), [ADR-019](ADR-019-tools-catalog-endpoint.md) (каталог tools)

## Контекст

Позиционирование сервиса (решение пользователя, 2026-06-04): **основная задача — агрегатор Claude для iOS («чистый чат»)**. Генерация сайтов (website-builder, server-side `site.*`) — **необязательная, второстепенная** фича.

Текущее состояние не отражает это позиционирование:
- `ChatRunRequest.projectId` — **обязателен** (`min_length=1`).
- `chat_sessions.project_id` — `TEXT NOT NULL` (external project id website-builder).
- `anthropic_tool_definitions()` всегда отдаёт **все 13 tools**, включая `site.*`, независимо от того, есть ли у сессии проект для записи сайта.

Это вынуждает клиента «чистого чата» придумывать фиктивный `projectId` и предлагает Claude инструменты записи сайта, которым некуда писать. Нужно сделать `projectId` опциональным и не предлагать `site.*` без проекта.

## Решение

### 1. `projectId` — опциональный атрибут сессии

`ChatRunRequest.projectId` становится **опциональным** (`str | None`, default `None`; при наличии — непустая строка). Семантика, фиксируемая **при создании сессии** (как `mode` и `assistantMode`, [ADR-012](ADR-012-assistant-mode-vs-billing-mode.md)):

- **Без `projectId`** → обычный чат-агрегатор. Сессия создаётся с `project_id = NULL`. Доступны client-side tools (`files.*`/`calendar.*`/`reminders.*`) по обычным правилам. Server-side `site.*` **НЕ предлагаются** Claude.
- **С `projectId`** → website-builder доступен. Сессия создаётся с `project_id = <строка>`. Набор tools включает `site.*` (как сейчас).

`projectId` — **per-session immutable**: фиксируется при создании, на последующих вызовах берётся из сессии, поле запроса игнорируется (см. §4).

### 2. Гейтинг `site.*` по наличию проекта

Набор tools, передаваемый Claude в `messages.create`, **зависит от наличия `project_id` у сессии**:

- `chat_sessions.project_id IS NULL` → tools = ALL_TOOL_NAMES **минус** `SERVER_SIDE_TOOLS` (`site.*`). Claude их не видит и физически не может вызвать.
- `chat_sessions.project_id IS NOT NULL` → tools = полный набор (включая `site.*`), как сейчас.

Нормативно: `anthropic_tool_definitions()` параметризуется флагом «проект доступен» (или эквивалентом — фильтрация `_ARGS_BY_TOOL` по `SERVER_SIDE_TOOLS`). Orchestrator в `_generate_loop` передаёт фильтрованный набор на основе `chat_sessions.project_id` сессии. Client-side tools (`files.*`/`calendar.*`/`reminders.*`) этим гейтом **не** затрагиваются.

**Гейт по `project_id` — НЕ единственный целевой фильтр `site.*`.** **Целевой контракт (Q-012-1 Open):** доступность `site.*` для Claude определяется **И-композицией двух ортогональных фильтров** одного и того же реестра:

- **Ось A — наличие проекта (этот ADR):** `project_id IS NOT NULL`;
- **Ось B — тип ассистента ([Q-012-1](../99-open-questions.md), [ADR-012 §25](ADR-012-assistant-mode-vs-billing-mode.md)):** `assistant_mode` допускает `site.*` (по дефолту Q-012-1: `code` — допускает, `chat` — реестр без `site.*`/`files.*`).

Целевое условие предложения `site.*` Claude:

```
offer(site.*) ⟺ (project_id IS NOT NULL) AND (assistant_mode допускает site.* по Q-012-1/ADR-012)
```

**Целевое поведение — И-композиция обеих осей.** **На текущем спринте реализована ось A (`project_id`)**: `anthropic_tool_definitions(include_server_side=...)` исключает `SERVER_SIDE_TOOLS` при `project_id IS NULL`. **Ось B (`assistant_mode`) отложена — [Q-012-1](../99-open-questions.md) Open и сознательно НЕ реализована**; при её закрытии она складывается по И с осью A тем же параметром `include_server_side` (фильтр реестра по `assistant_mode`). Целевой инвариант: ни одна из осей не является «единственной точкой фильтрации» `site.*` — ось A добавляется поверх оси B; аналогично `files.*` (целевой контракт) гейтится осью B независимо от `project_id`.

Защита от аномалии: если при `project_id IS NULL` Claude всё же вернёт `tool_use` с именем из `SERVER_SIDE_TOOLS` (не должно случиться, т.к. tool не предлагался) — это трактуется как upstream-аномалия (как неизвестное имя tool, [ADR-008](ADR-008-provider-tool-use-id.md)/`UnknownToolNameError`-семантика): backend не исполняет `site.*` без проекта, шаг завершается контролируемой ошибкой обработки tool_use, наружу как валидный tool **не** транслируется.

### 3. `chat_sessions.project_id` → nullable (миграция 0007, expand-only)

`ALTER COLUMN project_id DROP NOT NULL`. Существующие строки сохраняют значение; новые сессии без `projectId` → `NULL`. Без бэкфилла, без потери данных, без изменения существующих индексов. Подробные требования к миграции — для backend (см. next_steps).

### 4. Расхождение `projectId` запроса ↔ сессии (resume)

`projectId` — атрибут сессии, как `mode`/`assistantMode`. При продолжении существующей сессии (`sessionId` задан):
- источник истины — `chat_sessions.project_id`;
- значение `projectId` в теле запроса при resume **игнорируется** (не ошибка), ровно как сейчас игнорируется `mode`/`assistantMode` при resume.

Обоснование: единообразие с уже принятым паттерном session-fixed-атрибутов ([ADR-012](ADR-012-assistant-mode-vs-billing-mode.md)) — наименьшее изменение, без новых кодов ошибок и без расширения контракта. Документируется явно во всех контрактных файлах.

### 5. Биллинг/режимы/trial/policy — без изменений

Наличие/отсутствие `projectId` **не влияет** на policy/billing: 1 кредит = 1 сообщение и для «чистого чата», и для website-builder ([ADR-006](ADR-006-credit-billing-and-subscription-grant.md), [ADR-002](ADR-002-access-policy-state-machine.md) — без изменений).

### 6. `GET /v1/tools` — без изменений

Каталог [ADR-019](ADR-019-tools-catalog-endpoint.md) остаётся **полным техническим реестром** backend (13 tools, с `site.*`). Это перечень возможностей сервиса, а не per-session offer-set; runtime-гейтинг `site.*` — concern tool-loop'а (аналогично тому, как каталог не параметризуется `assistantMode`).

## Последствия

**Плюсы:**
- «Чистый чат» работает без проекта и без website-инструментов — соответствует позиционированию.
- Без проекта `site.*` недоступны → **нет поверхности IDOR** по проекту: `_external_project_id()` не вызывается, модель не может инициировать запись/чтение чужого проекта (ADR-011 IDOR-guard сохраняется и усиливается отсутствием самих tools). Согласовано с [05-security.md](../05-security.md).
- Expand-only миграция, обратная совместимость: клиент с `projectId` работает как раньше.

**Минусы / следствия:**
- `_external_project_id()` теперь вызывается только на ветке с непустым `project_id`; при `NULL` `site.*` не предлагаются, поэтому путь недостижим — добавляется defensive-guard (backend).
- Tool-набор перестаёт быть глобальной константой запроса — становится session-aware (фильтрация по `SERVER_SIDE_TOOLS`).
- Prompt caching: системный промт неизменен; меняется только `tools[]` — кэш tool-блока для chat-сессий (без `site.*`) и code/website-сессий различается, что ожидаемо и безвредно.

## Альтернативы (отклонены)

- **Оставить `projectId` обязательным, клиент шлёт пустышку.** Отклонено: загрязняет контракт, предлагает Claude бесполезные `site.*`, не отражает позиционирование.
- **Гейтить `site.*` ТОЛЬКО по `assistantMode=code`.** Отклонено как *единственный* критерий: `assistantMode` ортогонален website-builder ([ADR-012](ADR-012-assistant-mode-vs-billing-mode.md)); `code`-ассистент полезен и без проекта сайта, поэтому одного `assistant_mode=code` недостаточно для исполнимости `site.*` (писать некуда без `project_id`). Этот ADR добавляет ось наличия `project_id` **поверх** (целевого) фильтра по `assistant_mode` ([Q-012-1](../99-open-questions.md)/[ADR-012 §25](ADR-012-assistant-mode-vs-billing-mode.md)): в целевом контракте обе оси действуют по И (см. §2), а не взаимоисключающе. На текущем спринте реализована ось A (`project_id`); ось B (`assistant_mode`) — Q-012-1 Open, не реализована.
- **Ошибка при resume-сессии с другим `projectId`.** Отклонено: вводит новый код ошибки и расходится с уже принятым паттерном session-fixed-атрибутов (`mode`/`assistantMode` молча игнорируются при resume).
- **Удалить website-builder.** Отклонено: фича остаётся, понижается до опциональной в формулировках.

## Задачи для backend (next steps)

- **Миграция 0007** (expand-only): `ALTER COLUMN chat_sessions.project_id DROP NOT NULL` (§3). Без бэкфилла, без изменения индексов.
- **Контракт:** `ChatRunRequest.projectId` → `str | None` (default `None`; при наличии — непустая строка), session-immutable при resume (§1, §4).
- **Гейтинг tool-реестра — ось A (этот спринт), ось B отложена (§2):**
  - **Реализовано сейчас — ось A:** `project_id IS NOT NULL` гейтит `site.*` через `anthropic_tool_definitions(include_server_side=...)`. Это текущий scope ADR-022.
  - **Будущее — ось B (при закрытии [Q-012-1](../99-open-questions.md)):** добавить фильтр по `assistant_mode` ([ADR-012 §25](ADR-012-assistant-mode-vs-billing-mode.md), гейтит `site.*`/`files.*`) по логическому И с осью A, достроив целевой контракт `offer(site.*) ⟺ (project_id IS NOT NULL) AND (assistant_mode допускает site.*)`. Сейчас ось B сознательно НЕ реализована (Q-012-1 Open); scope текущего спринта = ось A (`project_id`).
- **Defensive-guard:** `_external_project_id()` вызывается только при непустом `project_id`; `tool_use` с `SERVER_SIDE_TOOLS` при `project_id IS NULL` — upstream-аномалия, не исполняется (§2).
