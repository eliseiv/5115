# ADR-019 — Каталог инструментов: `GET /v1/tools`

- Статус: Accepted
- Дата: 2026-06-02
- Связан с: [ADR-011](ADR-011-server-side-tools.md) (server-side tools), [ADR-008](ADR-008-provider-tool-use-id.md), [modules/chat-orchestrator/](../modules/chat-orchestrator/README.md), [API-REFERENCE.md §13](../API-REFERENCE.md#13-tool-протокол)

## Context

iOS-клиент должен знать, какие tools поддерживает backend, чтобы (а) реализовать client-side исполнение (`files.*`/`calendar.*`/`reminders.*`), (б) отличать client-side от server-side (`site.*`, исполняет backend сам, [ADR-011](ADR-011-server-side-tools.md)), (в) предупреждать пользователя о mutating-действиях. Сейчас этот реестр зашит в `src/app/chat/tools.py` (13 tools: `ALL_TOOL_NAMES`, `MUTATING_TOOLS`, `SERVER_SIDE_TOOLS`, `_ARGS_BY_TOOL`, `anthropic_tool_definitions()`) и доступен клиенту только из документации. Нужен машиночитаемый эндпоинт-каталог.

## Decision

**Добавить `GET /v1/tools`** — каталог всех доступных tools, источник — `src/app/chat/tools.py` (single source of truth, без дублирования списка).

### Контракт (см. [modules/chat-orchestrator/02-api-contracts.md](../modules/chat-orchestrator/02-api-contracts.md))

Для каждого tool (13 штук):
- `name` — **доменное имя с точкой** (`files.read`, `site.write_file`, …), как в публичном iOS-контракте (ТЗ §5). НЕ anthropic-underscore-имя (`files_read`) — то существует только на Anthropic-транспорте (BUG-3, [tools.py](../../src/app/chat/tools.py)).
- `description` — человекочитаемое описание (из `descriptions` в `anthropic_tool_definitions()`).
- `mutating` (bool) — из `MUTATING_TOOLS` (требует audit-записи при исполнении).
- `execution` (`"client"` | `"server"`) — `"server"` для tools из `SERVER_SIDE_TOOLS` (`site.*`, исполняет backend), `"client"` для остальных (исполняет iOS).
- `input_schema` — JSON Schema аргументов (из `_ARGS_BY_TOOL[name].model_json_schema()`, как в `anthropic_tool_definitions()`).

Ответ: `{ "tools": [ {name, description, mutating, execution, inputSchema}, ... ] }`. Порядок — детерминированный (по `_ARGS_BY_TOOL`).

### Авторизация — **JWT-protected** (как все `/v1/*`)

Каталог **не секретен**, но эндпоинт встроен в `/v1/*` контур и подчиняется его сквозным правилам ([api-gateway/02-api-contracts.md](../modules/api-gateway/02-api-contracts.md)): `Authorization: Bearer <JWT>` обязателен. Обоснование:
- Единообразие: все `/v1/*` (кроме `/v1/preview/*`) под JWT — не вводим исключение, не усложняем gateway middleware.
- Снижение API-surface для неаутентифицированных: каталог раскрывает форму tool-API, незачем отдавать его анонимно.
- Клиент к моменту запроса tools **уже** имеет JWT (получен через `/v1/auth/register`, [ADR-018](ADR-018-embedded-auth-issuer.md)) — дополнительной стоимости нет.
- Lazy-provisioning ([ADR-007](ADR-007-lazy-user-provisioning.md)) на read-only GET безвреден (создаёт пустого пользователя с дефолтами).

Метод — **`GET`** (чтение, кэшируемо, без побочных эффектов). Per-user rate-limit как у прочих read-эндпоинтов.

> Зависимость от `assistant_mode` (Q-012-1: какие tools доступны в chat vs code) **не** влияет на `/v1/tools`: эндпоинт возвращает **полный технический реестр backend** (что backend в принципе умеет), а не подмножество для конкретного режима. Фильтрация по `assistant_mode` — concern tool-loop'а ([ADR-012](ADR-012-assistant-mode-vs-billing-mode.md)), не каталога. Это зафиксировано, чтобы каталог не пришлось параметризовать.

## Consequences

**Положительные:**
- iOS получает машиночитаемый реестр; client/server-разделение и mutating-флаг явны.
- Single source of truth: эндпоинт читает `tools.py`, рассинхрон с tool-loop невозможен by construction.

**Отрицательные / ограничения:**
- Каталог статичен в пределах деплоя (меняется только с релизом backend) — кэшируем на клиенте; явный cache-invalidation не нужен.

## Alternatives

1. **Публичный (без JWT) `/v1/tools`.** Отвергнуто: ввело бы исключение в gateway-auth, раскрыло бы API-surface анонимно; выгоды нет (клиент уже с JWT).
2. **Только документация (API-REFERENCE), без эндпоинта.** Отвергнуто: не машиночитаемо, клиент дублирует список вручную → риск рассинхрона.
3. **Возвращать anthropic-underscore-имена.** Отвергнуто: публичный iOS-контракт использует dotted-имена; underscore — внутренняя деталь Anthropic-транспорта (BUG-3).
