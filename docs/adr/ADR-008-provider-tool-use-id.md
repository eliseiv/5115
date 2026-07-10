# ADR-008 — Раздельное хранение provider tool_use.id для согласованности continuation

- Статус: Accepted
- Дата: 2026-05-25
- Связан с: [ADR-005](ADR-005-idempotency-ledger.md) (tool-result/идемпотентность), [03-data-model.md](../03-data-model.md), [modules/chat-orchestrator/03-architecture.md](../modules/chat-orchestrator/03-architecture.md), [modules/chat-orchestrator/04-data-model.md](../modules/chat-orchestrator/04-data-model.md)

## Context

Tool-loop оркестратора многораундовый: `/chat/run` → `tool_call` → `/chat/tool-result` → (повторный `messages.create` с tool_result-блоком) → … → `assistant_message`. Anthropic Messages API накладывает жёсткий инвариант: при continuation `tool_result.tool_use_id` должен **точно** совпадать с `tool_use.id` соответствующего блока assistant-хода в реплеемой истории `messages`.

Реальный Anthropic `tool_use.id` — строка вида `toolu_01...` (произвольный формат провайдера, **не** UUID).

Backend выводил публичный доменный `toolCallId` из id ответа Claude:
```
tool_call_id = uuid.UUID(first['id']) if _is_uuid(first['id']) else uuid.uuid4()
```
Для реального Claude `first['id']` не-UUID → ветка `else` генерировала свежий `uuid4`. Дальнейшее:
- `chat_steps.payload` реплеился **дословно** (содержал raw `toolu_...`);
- `/chat/tool-result` `_build_messages` строил `tool_result.tool_use_id` из доменного `toolCallId` (= `uuid4`).

Результат: `tool_use.id` в истории (`toolu_...`) ≠ `tool_result.tool_use_id` (`uuid4`) → Anthropic `400 invalid_request_error` → backend `502`. **Continuation (любой второй раунд tool-loop) сломан в production** (BUG-4, CRITICAL).

Дефект не ловился на пирамиде: fake/мок Anthropic-клиента возвращал UUID-образный `tool_use.id`, поэтому `_is_uuid` давал `true` и id случайно совпадал.

Рассмотрены два варианта устранения.

- **(а) Раздельное хранение provider id.** Новая колонка `tool_calls.provider_tool_use_id (TEXT)`: при разборе `tool_use` сохраняется raw `toolu_...`; доменный `id` (UUID) генерируется независимо. При continuation `tool_result.tool_use_id` = `provider_tool_use_id`; `payload` реплеится как есть. Требует миграции (data-model change).
- **(б) Переписывание id при реплее (code-only).** Без новой колонки: при реплее каждый `tool_use.id` в сохранённом `payload` переписывается на доменный UUID, и `tool_result.tool_use_id` тоже доменный — обе стороны согласованы на доменном id (Anthropic важно совпадение, не формат).

## Decision

**Принят вариант (а): отдельная колонка `tool_calls.provider_tool_use_id (TEXT, NOT NULL)`.**

1. **При генерации шага (`/chat/run`, разбор `tool_use`):**
   - `tool_calls.id` = свежий `uuid4`. Вывод доменного id из anthropic `tool_use.id` **запрещён**; ветка `_is_uuid` удаляется.
   - `tool_calls.provider_tool_use_id` = raw `tool_use.id` блока (сохраняется как непрозрачная строка, без валидации формата).
   - `chat_steps.payload` сохраняет content blocks дословно (raw `tool_use.id`).
   - Наружу (`toolCall.id`) — только доменный UUID.

2. **При continuation (re-entry `/chat/run` и `/chat/tool-result`, сборка `messages`):**
   - Прошлые assistant-ходы реплеятся из `payload` дословно (raw `tool_use.id` не переписывается).
   - tool_result-блок текущего раунда: `tool_use_id = tool_calls.provider_tool_use_id` (найден по доменному `toolCallId`). Никогда не доменный UUID, никогда не свежий uuid4.

Инвариант пространств id: domain `toolCallId` (UUID) — публичный (iOS, API, `/chat/tool-result`); provider `tool_use.id` (`toolu_...`) — внутренний (только Anthropic history). Связь 1:1 в записи `tool_calls`. Полный нормативный контракт — [chat-orchestrator/03-architecture.md § Согласованность tool_use.id](../modules/chat-orchestrator/03-architecture.md#согласованность-tool_useid-в-истории-anthropic-bug-4).

## Rationale (почему (а), а не (б))

- **Без потери информации в источнике.** (б) выбрасывает raw `toolu_...` при генерации и реконструирует согласованность трансформацией persisted истории на каждом раунде. (а) сохраняет provider id один раз — continuation читает готовое значение.
- **Минимум логики в hot path continuation.** (б) требует на каждом раунде обходить весь `payload`, находить все `tool_use` блоки, переписывать их id и сопоставлять с записями `tool_calls`. При parallel tool use (несколько `tool_use` в одном ходе) сопоставление "блок ↔ tool_call" нетривиально; ошибка → снова 400. (а) даёт детерминированное `provider_tool_use_id` по `toolCallId` без обхода истории.
- **Инвариант выражен в схеме, а не в неявном преобразовании.** Исходный баг — следствие неявной id-логики, незаметной тестам. Персистентная колонка делает связь явной и устойчивой к регрессу/рефакторингу реплея.
- **Цена (а)** — одна nullable-by-nature, но `NOT NULL` колонка + одна миграция. Для production-критичного пути continuation надёжность приоритетнее экономии миграции ([00-vision](../00-vision.md) — корректность continuation как функциональное требование tool-loop, AC-4).

## Consequences

- **Положительные:** continuation работает с реальным Claude; контракт согласованности id явный и тестируемый; parallel tool use поддержан поблочно; публичный iOS-контракт (`toolCall.id` = UUID) не меняется (не breaking change).
- **Отрицательные / издержки:** добавлена колонка + миграция; backend обязан заполнять `provider_tool_use_id` при каждом создании `tool_calls` (новый инвариант). Для записей `tool_calls`, созданных до миграции (если такие есть в окружении), continuation невозможен — но это dev-данные, prod ещё не запущен (write-path чинился в BUG-1, [ADR-007](ADR-007-lazy-user-provisioning.md)).
- **Тестовое требование (нормативно):** fake Anthropic-клиента во всех тестах обязан отдавать `tool_use.id` в формате `toolu_...` (не UUID-образный). E2E-тест continuation проверяет равенство `tool_result.tool_use_id` raw id и должен падать на старой реализации. См. [chat-orchestrator/09-testing.md](../modules/chat-orchestrator/09-testing.md).

## Migration

Alembic, expand/contract под rolling update ([07-deployment.md §Миграции](../07-deployment.md)). Две миграции:

**Expand (deploy N):** колонка nullable, код выката N начинает её заполнять. Старые pod'ы (N-1) без колонки rolling-совместимы.
```sql
ALTER TABLE tool_calls
    ADD COLUMN provider_tool_use_id TEXT;  -- nullable: backward-compatible
```

**Contract (deploy N+1, после полного выката кода, заполняющего колонку):**
```sql
-- Бэкофилл недоступен: raw anthropic id ранее не сохранялся. Незавершённые
-- pending tool_calls без provider_tool_use_id непригодны для continuation
-- (prod не запущен — это dev/test-данные, очистить перед contract).
ALTER TABLE tool_calls
    ALTER COLUMN provider_tool_use_id SET NOT NULL;
```

> Если окружение однопроцессное/без rolling (как минимум на dev) — допустимо объединить в одну миграцию. Логическая цель — итоговый `NOT NULL`, отражённый в [03-data-model.md](../03-data-model.md). Backfill старых строк невозможен (информация утеряна на источнике); такие записи для continuation непригодны.

## Alternatives

- **(б) Переписывание id при реплее** — отклонён: хрупкая трансформация persisted истории на hot path, нетривиальное сопоставление при parallel tool use, инвариант остаётся неявным (риск повторения исходного класса бага).
- **Парсить/нормализовать anthropic id в UUID** — невозможно: `toolu_...` не несёт UUID-семантики, любая нормализация рвёт согласованность с реплеемой историей.
