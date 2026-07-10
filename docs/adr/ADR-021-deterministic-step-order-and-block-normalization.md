# ADR-021 — Детерминированный порядок шагов сессии (монотонный `seq`) + нормализация content-блоков перед персистом

- Статус: Accepted
- Дата: 2026-06-04
- Связан с: [ADR-008](ADR-008-provider-tool-use-id.md) (provider tool_use.id / согласованность continuation), [ADR-005](ADR-005-idempotency-ledger.md), [ADR-011](ADR-011-server-side-tools.md) (server-side tool-loop), [03-data-model.md](../03-data-model.md), [modules/chat-orchestrator/03-architecture.md](../modules/chat-orchestrator/03-architecture.md), [modules/chat-orchestrator/04-data-model.md](../modules/chat-orchestrator/04-data-model.md)

## Context

### Проблема 1 — недетерминированный порядок реконструкции при равном `created_at` (BUG-5, CRITICAL)

Реконструкция истории сессии (`_build_messages`, `orchestrator.py`) читает `chat_steps` через `list_steps`, который сортирует по `(created_at, id)`. На server-side ветке tool-loop (`_execute_server_side_tool`, website-builder `site.*`, [ADR-011](ADR-011-server-side-tools.md)) assistant-шаг (`tool_use`) и tool-шаг (`tool_result`) записываются в `chat_steps` в **одной транзакции** → у обеих строк одинаковый Postgres транзакционный `now()` в `chat_steps.created_at`.

При равном `created_at` tie-break идёт по `id` — это UUID v4 (`gen_random_uuid()`), **не монотонный**. С вероятностью ~50% `tool_result`-строка получает меньший UUID и сортируется **раньше** породившего её `tool_use`-шага. Тогда `_build_messages` собирает `messages` с tool_result-сообщением **перед** assistant-tool_use → orphan `tool_result` → Anthropic `400 invalid_request_error` → backend `502`.

Client-side tool-loop не затронут: там `tool_use` (запрос `/chat/run`) и `tool_result` (запрос `/chat/tool-result`) пишутся в **разных** транзакциях/HTTP-запросах, поэтому `created_at` различаются и порядок корректен.

Repository уже фиксировал ненадёжность `created_at` при transaction-time `now()` (комментарий у `next_step_after`, `repository.py`): несколько строк одной транзакции получают идентичный `created_at`, и `(created_at, id)` не гарантирует порядок вставки.

Корень: **порядок шагов сессии нельзя выводить из `created_at`** — это transaction-time timestamp, не монотонный по вставке, а UUID-tie-break случаен.

### Проблема 2 — нестандартное SDK-поле в реплее (минор, не причина 400)

Сохранённый assistant `tool_use` content-block содержит служебное поле SDK `"caller":{"type":"direct"}` — попадает из `block.model_dump()` (`anthropic_client.py`) в `chat_steps.payload` и далее дословно реплеится на wire в `messages` к Anthropic. Это не wire-валидное поле Anthropic Messages API; оно не вызывает 400 (API игнорирует), но это мусор в запросе и нарушение инварианта «в payload — только wire-валидные блоки».

### Контракт наружу

`/v1/chat/run` и `/v1/chat/tool-result` — это **внутренний** фикс реконструкции истории. Публичные контракты (request/response, `toolCall.id` UUID, поля `steps[]`) **не меняются**.

## Decision

### 1. Монотонная колонка порядка `chat_steps.seq` (глобальный identity)

Добавляется колонка `chat_steps.seq BIGINT GENERATED ALWAYS AS IDENTITY` (глобальный автоинкремент Postgres, эквивалент `BIGSERIAL`). Значение присваивается БД при INSERT; в рамках одной транзакции несколько INSERT получают возрастающие `seq` **в порядке выполнения вставок** — то есть `tool_use` (вставлен первым) всегда получит меньший `seq`, чем `tool_result` (вставлен после него).

- **`list_steps` и `next_step_after` сортируют по `seq ASC` (а не `(created_at, id)`).** `created_at` остаётся в таблице как информационный timestamp (отдаётся в `steps[].createdAt`), но **порядок реконструкции и поиска следующего шага определяется `seq`**.
- Индекс `ix_steps_session_created (session_id, created_at)` заменяется на `ix_steps_session_seq (session_id, seq)` — покрывает запросы реконструкции по сессии в порядке `seq`.

**Глобальный identity, не per-session счётчик.** Выбран глобальный sequence, а не per-session `step_index`:
- Sequence атомарен и конкурентно-безопасен на уровне БД без явной блокировки/`SELECT ... FOR UPDATE` per-session (per-session счётчик требовал бы сериализации вставок в сессию или advisory lock — лишняя сложность и contention).
- Для упорядочивания внутри сессии важна **монотонность относительного порядка**, а не плотность нумерации; гэпы в `seq` (от других сессий, откатов) безвредны — сортировка `WHERE session_id=:s ORDER BY seq` корректна при любых гэпах.
- Порядок нескольких шагов одной транзакции сохраняется: identity присваивается в момент каждого INSERT в порядке их выполнения.

**Инвариант (нормативно):** порядок шагов в сессии определяется монотонным `chat_steps.seq`, **НЕ** `created_at`. Любая реконструкция истории и поиск «следующего шага» используют `seq`. `created_at` — информационный timestamp, не порядковый ключ.

### 2. Нормализация content-блоков перед персистом

При сохранении assistant content-блоков в `chat_steps.payload` хранятся **только wire-валидные поля Anthropic Messages API**. Служебные/нестандартные поля SDK (в частности `caller`) удаляются из `block.model_dump()` **перед** записью в `chat_steps.payload`, чтобы они не попадали в реплей на wire.

- Нормализация выполняется на границе персиста (при сборке payload из ответа Anthropic) — единый источник чистых блоков для всех последующих реплеев.
- Для `tool_use` блока сохраняются wire-поля: `type`, `id`, `name`, `input` (raw `tool_use.id` сохраняется дословно — инвариант [ADR-008](ADR-008-provider-tool-use-id.md) не нарушается). Поля вне wire-схемы Anthropic (`caller` и любые будущие SDK-аннотации) отбрасываются.
- Требование к backend: нормализация — allowlist/denylist по wire-схеме блока, а не точечное удаление одного ключа `caller` (устойчивость к новым служебным полям SDK).

## Rationale

### Почему глобальный `seq`, а не сортировка по `(created_at, id)` с монотонным id

- `created_at` = transaction-time `now()` → одинаков для шагов одной транзакции; не лечится индексом.
- Сделать `id` монотонным (например ULID/UUIDv7) лечит tie-break, но (а) `id` — публичный доменный ключ строки, менять его генерацию рискованнее, чем добавить выделенную порядковую колонку; (б) семантика «порядок» должна быть выражена явной колонкой, а не побочным свойством PK. Явный `seq` делает инвариант читаемым и устойчивым к рефакторингу генерации id.

### Почему глобальный identity, а не per-session `step_index`

- Конкурентность: identity-sequence не требует блокировки сессии; per-session счётчик при параллельных вставках в одну сессию требует сериализации (advisory lock / `FOR UPDATE`) — contention и сложность.
- Достаточность: для `ORDER BY seq` внутри `WHERE session_id=:s` плотность нумерации не нужна, только монотонность.

### Почему нормализация на границе персиста

- Единичная точка очистки (при сборке payload из ответа) гарантирует, что **все** последующие реплеи читают уже чистые блоки — не нужно нормализовать на каждом реплее (hot path continuation остаётся дешёвым, согласовано с rationale [ADR-008](ADR-008-provider-tool-use-id.md)).

## Consequences

- **Положительные:** server-side tool-loop continuation детерминирован и корректен (нет orphan tool_result → нет 400/502); инвариант порядка выражен явной колонкой и тестируем; реплей не несёт служебных SDK-полей; публичные контракты не меняются (не breaking).
- **Отрицательные / издержки:** добавлена колонка + миграция (`0006`) с backfill; backend обязан (а) сортировать по `seq` в `list_steps`/`next_step_after`, (б) нормализовать блоки перед персистом — новые инварианты. `created_at` больше не порядковый ключ (его сортировка в коде заменяется на `seq`).
- **Тестовое требование (нормативно):** тест server-side tool-loop должен записывать `tool_use` и `tool_result` в одной транзакции и проверять, что реконструированные `messages` идут в порядке `tool_use → tool_result` независимо от значений `id`/`created_at`. Должен падать на старой `(created_at, id)`-сортировке. Тест нормализации проверяет отсутствие `caller` (и любых не-wire полей) в `chat_steps.payload` и в собранных `messages`. См. [modules/chat-orchestrator/09-testing.md](../modules/chat-orchestrator/09-testing.md).

## Migration

Alembic, миграция `0006`, цепочка `0001`→…→`0005`→`0006` (expand-only под rolling update, [07-deployment.md §Миграции](../07-deployment.md)).

Итоговое состояние колонки — `seq BIGINT GENERATED ALWAYS AS IDENTITY`, `NOT NULL`, индекс `ix_steps_session_seq (session_id, seq)` — отражено в [03-data-model.md](../03-data-model.md). Но напрямую `ADD COLUMN ... GENERATED ALWAYS AS IDENTITY` + `UPDATE` backfill в Postgres **неисполнимо**: `UPDATE` колонки `GENERATED ALWAYS` запрещён («column can only be updated to DEFAULT»). Поэтому колонка сначала добавляется как `GENERATED BY DEFAULT` (чтобы backfill-`UPDATE` был разрешён), а в конце фиксируется как `GENERATED ALWAYS`. Все шаги выполняются **атомарно в одной транзакции** миграции (Alembic оборачивает `upgrade()`/`downgrade()` в транзакцию).

Требования к миграции (для backend):

1. **Добавить колонку как `GENERATED BY DEFAULT` identity** — не `GENERATED ALWAYS`, иначе backfill-`UPDATE` (шаг 2) будет отвергнут Postgres:
   ```sql
   ALTER TABLE chat_steps
       ADD COLUMN seq BIGINT GENERATED BY DEFAULT AS IDENTITY;
   ```
   Identity заполняет `seq` для всех **существующих** строк сразу при добавлении (Postgres присваивает значения в недетерминированном физическом порядке) — колонка фактически уже без NULL; шаг 2 переписывает её в детерминированный порядок.

2. **Backfill детерминированного исторического порядка** — чтобы исторические сессии сохранили совместимый порядок реконструкции. `UPDATE` переписывает `seq` существующих строк через `ROW_NUMBER()` в порядке `(created_at, id)` (порядок, использовавшийся ранее, включая `id`-tie-break для строк одной транзакции):
   ```sql
   WITH ordered AS (
       SELECT id, ROW_NUMBER() OVER (ORDER BY created_at, id) AS rn
       FROM chat_steps
   )
   UPDATE chat_steps cs
   SET seq = ordered.rn
   FROM ordered
   WHERE cs.id = ordered.id;
   ```
   Для строк одной транзакции с равным `created_at` порядок backfill совпадает с прежним поведением (`id`-tie-break) — сохраняется ровно тот же исторический порядок, что был до фикса (не хуже); новые шаги пишутся уже корректно.

3. **Продвинуть identity-sequence выше `max(seq)`** — backfill переписал значения «из-под» identity-счётчика, поэтому его указатель нужно подвинуть, иначе новые вставки столкнутся с backfill'енными значениями. `COALESCE` обрабатывает пустую таблицу (рестарт с 1):
   ```sql
   SELECT setval(
       pg_get_serial_sequence('chat_steps', 'seq'),
       (SELECT COALESCE(MAX(seq), 0) + 1 FROM chat_steps),
       false
   );
   ```

4. **Зафиксировать колонку как `GENERATED ALWAYS`** (соответствует ORM-модели `Identity(always=True)`: код никогда не задаёт `seq`, БД всегда присваивает его при INSERT). Делается **после** backfill, поскольку `ALWAYS` запретил бы `UPDATE`:
   ```sql
   ALTER TABLE chat_steps ALTER COLUMN seq SET GENERATED ALWAYS;
   ```

5. **`NOT NULL`** — подтверждающий после backfill (no-op после identity + backfill; оставлен для явности инварианта):
   ```sql
   ALTER TABLE chat_steps ALTER COLUMN seq SET NOT NULL;
   ```

6. **Индекс:** drop `ix_steps_session_created (session_id, created_at)` → create `ix_steps_session_seq (session_id, seq)`. `ix_steps_message_step (message_step_id)` — не трогать.

7. **Downgrade обратим:** восстановить старый индекс `ix_steps_session_created (session_id, created_at)` и `DROP COLUMN seq` (identity-sequence удаляется вместе с колонкой автоматически).

> Все шаги 1–6 атомарны в одной транзакции миграции (Alembic), отдельной фазировки под rolling не требуется. Логическая цель — итоговый `GENERATED ALWAYS AS IDENTITY` + `NOT NULL seq` + индекс `(session_id, seq)`, отражённые в [03-data-model.md](../03-data-model.md). Prod ещё не несёт нагрузки на server-side tool-loop (live-генерация заблокирована отключённой org Anthropic — см. memory/deployment-state), поэтому объём backfill минимален.

Нормализация content-блоков (Decision §2) — **код-фикс без миграции**: чистит вновь записываемые payload. Исторические payload со служебным `caller` остаются как есть (не вызывают 400; backfill payload не требуется). При необходимости — точечная очистка существующих payload как отдельная необязательная операция (не блокер; не входит в `0006`).

## Alternatives

- **Монотонный `id` (UUIDv7/ULID) вместо `seq`** — отклонён: меняет генерацию публичного PK; смешивает идентичность и порядок в одной колонке; менее явный инвариант.
- **Per-session `step_index` (INT)** — отклонён: требует сериализации вставок в сессию (advisory lock/`FOR UPDATE`) для конкурентной корректности; плотная нумерация не нужна для упорядочивания.
- **Писать `tool_use` и `tool_result` server-side ветки в разных транзакциях** (как client-side) — отклонён: рвёт атомарность записи шага tool-loop (риск частичного состояния при сбое между вставками), лечит только частный случай (server-side), не устраняет принципиальную ненадёжность `created_at` как порядкового ключа.
- **Добавить искусственный сдвиг `created_at` между вставками** — отклонён: хак, не выражает инвариант, ломается при любой будущей пакетной вставке.
