# ADR-040 — Редактирование отправленного сообщения: усечение истории хода + регенерация

- Статус: Accepted
- Дата: 2026-06-19
- Расширяет: [ADR-021](ADR-021-deterministic-step-order-and-block-normalization.md) (монотонный `chat_steps.seq`), [ADR-023](ADR-023-sync-ids-in-chat-response.md) (`messageStepId`/`stepId` наружу)
- Связано: [ADR-005](ADR-005-idempotency-ledger.md), [ADR-006](ADR-006-credit-billing-and-subscription-grant.md), [ADR-022](ADR-022-optional-project-and-tool-gating.md), [ADR-036](ADR-036-workspaces-implementation.md), [ADR-038](ADR-038-move-chat-to-workspace.md), [ADR-039](ADR-039-optional-message-with-attachments.md)
- Модуль: [chat-orchestrator](../modules/chat-orchestrator/README.md)

## Context

iOS-flow «редактирование уже отправленного сообщения»: пользователь меняет текст ранее
отправленного сообщения, старый ход (его ответ и всё, что было после) отбрасывается, и диалог
продолжается с новой формулировкой. Это стандартный паттерн чат-клиентов (edit → regenerate).

Текущая модель истории (источник истины — `src/app/models/tables.py`,
[03-data-model.md](../03-data-model.md)):

- `chat_steps` — шаги диалога: `id` (PK uuid), **`seq`** (BIGINT `Identity(always=True)`, монотонный
  порядок вставки, [ADR-021](ADR-021-deterministic-step-order-and-block-normalization.md)),
  `session_id` (FK `chat_sessions.id`, `ON DELETE CASCADE`), **`message_step_id`** (uuid хода,
  НЕ FK), `role` (`user`|`assistant`|`tool`), `payload`, `usage`.
- `tool_calls` — вызовы инструментов: `session_id` (FK `chat_sessions.id`, `ON DELETE CASCADE`),
  **`message_step_id`** (plain uuid, **НЕ FK на шаг**), `status`, и т.д.
- **`tool_calls` НЕ имеет FK на `chat_steps`.** Удаление строк `chat_steps` каскадно `tool_calls`
  **НЕ** удаляет — каскады обеих таблиц завязаны только на `chat_sessions`. Это ключевой факт для
  семантики усечения (см. ниже).
- Наружу клиент уже получает идентификаторы для адресации хода:
  `/chat/run`/`/chat/tool-result` возвращают `messageStepId` + `stepId`
  ([ADR-023](ADR-023-sync-ids-in-chat-response.md)); `GET /v1/chats/{id}` отдаёт `steps[].id` и
  `steps[].messageStepId`. `seq` наружу **не** отдаётся (внутренний порядковый ключ).
- `orchestrator.run()` уже идемпотентно дебетует кредит по `(user_id, message_step_id)`
  ([ADR-005](ADR-005-idempotency-ledger.md)/[ADR-006](ADR-006-credit-billing-and-subscription-grant.md)):
  новый ход = новый `message_step_id` = новый дебит.

**Решение пользователя (согласовано):** редактирование реализуется **одним** вызовом
существующего `POST /v1/chat/run` через новое поле **`editMessageStepId`** — атомарно (усечение +
новый ход в одной транзакции запроса). Отдельный endpoint удаления сообщения без регенерации — вне
scope ([Q-040-1](../99-open-questions.md)).

## Decision

### 1. Контракт: `ChatRunRequest.editMessageStepId: uuid | None`

Новое **опциональное** поле в `POST /v1/chat/run`. Семантика: **усечь** историю сессии от хода
`editMessageStepId` (его user-шаг и **всё, что после**) и сгенерировать **новый** ход с переданным
`message`/`attachments`/`context`.

Валидация:

- **`editMessageStepId` без `sessionId` → `422`** (`"editMessageStepId requires sessionId"`).
  Редактирование возможно только в существующей (resume) сессии; нельзя сочетать с созданием новой
  сессии.
- **`editMessageStepId` присутствует, но сессия чужая/не существует/истекла → `404`** (существующая
  resume-логика `get_or_create_session`/`get_session`: на чужую/отсутствующую/истёкшую сессию resume
  не выполняется). Для edit это означает невозможность редактировать ход несуществующего/чужого
  чата — изоляция не нарушается.
- **`message_step_id` не существует в сессии ИЛИ в нём нет user-шага → `404 message_not_found`.**
  Anchor определяется по `role='user'`; если `editMessageStepId` указывает на assistant/tool-шаг
  (нет user-шага с таким `message_step_id`) — тоже `404 message_not_found` (см. §4в).
- **Нельзя сочетать с созданием новой сессии** (следствие требования `sessionId`): edit всегда
  resume.

Поле session-агностично к прочим session-fixed полям: `mode`/`assistantMode`/`model`/`projectId`/
`workspaceProjectId` при edit берутся из **существующей** сессии (resume — поля запроса игнорируются,
как сегодня). Edit не меняет атрибуты сессии, только усекает историю и добавляет новый ход.

### 2. Усечение (точная семантика)

В транзакции запроса `/chat/run`, **до** `add_step(user)` нового хода:

1. **anchor** = минимальный `chat_steps.seq` среди шагов сессии с `role='user'` И
   `message_step_id = editMessageStepId`. Если такого user-шага нет, метод усечения возвращает
   `None` (кол-во удалённых шагов не определено) → caller отдаёт `404 message_not_found`.
2. **Удалить все `chat_steps`** сессии с `seq >= anchor` (редактируемый user-шаг + его
   assistant/tool-шаги + все последующие ходы).
3. **Явно удалить `tool_calls`** усечённых ходов — по их `message_step_id`
   (`DELETE FROM tool_calls WHERE session_id = :sid AND message_step_id IN (<message_step_id'ы
   удаляемых шагов>)`). **Обязательно**, т.к. FK `tool_calls` завязан на `session_id`, а **не** на
   `chat_steps` → каскад при удалении шагов не срабатывает; без явного удаления остались бы
   осиротевшие `tool_calls` (битые барьеры/replay).
4. Выполнить обычный поток нового хода: `add_step(user, message_step_id=<новый uuid4>)` →
   policy → `_generate_loop`.

Всё — в **одной транзакции запроса** (общий commit с генерацией, как сегодня в `run()`): усечение +
новый user-шаг атомарны относительно друг друга (промежуточные коммиты внутри `_generate_loop`
происходят уже **после** усечения и записи нового user-шага). **Миграции БД нет** (только новые
DELETE-запросы в репозитории + новое поле схемы запроса).

Усечение использует `seq` ([ADR-021](ADR-021-deterministic-step-order-and-block-normalization.md))
как единственный надёжный порядковый ключ: `created_at` равен для шагов одной транзакции (tie-break
по случайному UUID), поэтому `>= anchor по seq` — корректная граница «этот ход и всё после».

### 3. Биллинг (refund-policy)

Регенерация — **обычный ход** с новым `message_step_id` → **новый дебит 1 кредита**
([ADR-006](ADR-006-credit-billing-and-subscription-grant.md), идемпотентность по
`(user_id, message_step_id)` сохраняется). **Возврата за удалённый старый ход НЕТ**: кредит за уже
сгенерированный (ныне усечённый) ход потреблён и не возвращается. Refund-policy зафиксирована:
**no-refund on edit** — редактирование тарифицируется как новое сообщение. Обоснование: простота
ledger-инвариантов (дебит — append-only, [ADR-005](ADR-005-idempotency-ledger.md)), отсутствие
反-абуза «edit-цикл для бесплатной регенерации». Открытый вопрос на пересмотр — [Q-040-2](../99-open-questions.md).

### 4. Edge-кейсы

- **(а) Редактирование ПЕРВОГО сообщения чата.** anchor = первый user-шаг сессии → усечение удаляет
  **всю** историю. Сессия становится пустой, но **существует** (строка `chat_sessions` не удаляется)
  → `ctx.is_new = False`. Следствия:
  - **Workspace-файлы НЕ переинъектируются** (turn-0-only, вариант a
    [ADR-038 §3.2](ADR-038-move-chat-to-workspace.md)): `is_new=False` → ветка
    `context_for_session` не вызывается, файлы не подаются. Это **приемлемо и зафиксировано**:
    симметрично с поведением перенесённого чата и inline-attachments
    ([ADR-020](ADR-020-inline-base64-attachments-mvp.md)). Файлы доступны только чату, начавшему
    генерацию в воркспейсе изначально. Запрос инлайн-attachments нового (отредактированного) хода
    подаётся как обычно (turn-0 нового хода).
  - **`instructions` инъектируются как обычно** — на каждом ходе сессии с workspace, развязано от
    `is_new` ([ADR-038 §3](ADR-038-move-chat-to-workspace.md), `instructions_for_session`). Так
    отредактированный первый ход всё равно получает project-instructions.
  - Если осмысленность «файлы только у изначального чата» окажется неудобной — пересмотр через
    [Q-040-3](../99-open-questions.md) (та же ось, что Q-038-1).
- **(б) Редактируемый ход или последующий с ОТКРЫТЫМ tool-loop** (есть pending `tool_calls`,
  незакрытый барьер [ADR-025](ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)). Усечение
  удаляет эти шаги и их `tool_calls` (§2 шаг 3) → **никаких осиротевших `tool_calls` и незакрытых
  барьеров**. Новый ход начинается с чистой границы. Клиент, у которого «завис» tool_call, просто
  редактирует сообщение — backend корректно сбрасывает незавершённый ход.
- **(в) `editMessageStepId` указывает на assistant/tool-шаг, а не на user-ход.** anchor ищется
  **строго по `role='user'`**. Если для данного `message_step_id` нет user-шага → `404
  message_not_found` (нельзя «редактировать» ответ ассистента; редактируется только сообщение
  пользователя). На практике `message_step_id` хода всегда имеет user-шаг (генерируется в `run()`
  для user-сообщения), поэтому штатно валидный `editMessageStepId` всегда резолвится; защита от
  передачи id, не соответствующего user-сообщению.
- **(г) policy-block отредактированного хода.** Усечение выполняется **до** policy-чека (в той же
  транзакции, что и `add_step(user)` нового хода) — как и в обычном потоке user-шаг персистится до
  policy ([ADR-002](ADR-002-access-policy-state-machine.md)). Если отредактированный ход блокируется
  policy (`status=blocked`), усечённая старая история **не восстанавливается** (edit трактуется как
  обычный ход). На старте усечение при block **не откатывается** — established-поведение; пересмотр
  (откат усечения при policy-block) → [Q-040-4](../99-open-questions.md).

### 5. Изоляция

Усечение выполняется **только** в сессии, проверенной на принадлежность пользователю (`sub`) —
через ту же `get_session(session_id, user_id)`/resume-логику, что и обычный `/chat/run`. **Нельзя
усечь чужой чат**: чужая сессия → resume не выполняется → `404` (см. §1). Все DELETE-запросы
скоупятся по `session_id` уже проверенной сессии.

### 6. Обратная совместимость

Без поля `editMessageStepId` `POST /v1/chat/run` **не меняется** — поведение полностью идентично
текущему. Поле опционально (`default=None`), аддитивно. Старые клиенты не затронуты. Миграции нет.

## Consequences

- **Плюсы:** один атомарный вызов для edit+regenerate; переиспользование существующего пайплайна
  (`run()` → policy → `_generate_loop` → дебит по новому `message_step_id`); корректная очистка
  `tool_calls` без осиротевших строк; изоляция и идемпотентность не нарушены; без миграции.
- **Минусы / границы:** удалённый ход безвозвратен (no soft-delete истории — append-only ledger
  сохраняется, но `chat_steps`/`tool_calls` физически удаляются); no-refund (edit тарифицируется
  как новый ход); workspace-файлы не реинъектируются при edit первого сообщения (вариант a). Все
  зафиксированы как принятые компромиссы / Q-040-*.
- **Backend-доработка** (см. [chat-orchestrator/07-implementation-phases.md](../modules/chat-orchestrator/07-implementation-phases.md)
  и подробные указания ниже): поле + валидатор схемы; метод репозитория
  `truncate_from_message_step` (возврат `int | None`; `None` → `404 message_not_found`) с явным
  удалением `tool_calls`; выделенный `MessageNotFoundError` (`code="message_not_found"`); точка
  вызова в `run()` до `add_step(user)`; прокидка из роутера. Тестируется qa (offline): усечение по seq, удаление
  tool_calls, 422/404-валидации, edit-первого-сообщения, edit при открытом tool-loop, новый дебит.

## Указания backend (нормативно)

1. **Схема** (`src/app/schemas/chat.py`, `ChatRunRequest`): добавить поле
   `editMessageStepId: uuid.UUID | None = Field(default=None, description=...)`. В
   `_check_sizes` (`model_validator(mode="after")`) добавить правило:
   `if self.editMessageStepId is not None and self.sessionId is None: raise ValueError(
   "editMessageStepId requires sessionId")` → `422`. Прочие правила (message/attachments §[ADR-039]
   и size-лимиты) — без изменений; при edit действует то же требование «message непуст ИЛИ ≥1
   attachment».
2. **Репозиторий** (`src/app/chat/repository.py`, `ChatRepository`): новый метод
   `async def truncate_from_message_step(self, session_id, message_step_id) -> int | None`:
   - SELECT anchor: `min(seq)` шага `session_id == sid, message_step_id == msid, role == 'user'`.
     None → вернуть `None` (user-шаг не найден; caller → `404 message_not_found`).
   - Собрать `message_step_id` всех шагов с `seq >= anchor` (для очистки tool_calls) **или** просто
     удалять `tool_calls` по `seq`-границе через подзапрос: `DELETE FROM tool_calls WHERE
     session_id = :sid AND message_step_id IN (SELECT DISTINCT message_step_id FROM chat_steps
     WHERE session_id = :sid AND seq >= :anchor)` — выполнить **до** удаления шагов (подзапрос
     читает ещё существующие шаги).
   - `DELETE FROM chat_steps WHERE session_id = :sid AND seq >= :anchor`.
   - `flush()` (commit делает `run()` общим коммитом хода). Вернуть **кол-во удалённых
     `chat_steps`** (`int`).
   - Контракт возврата: `int` (≥0, число усечённых шагов) при найденном user-шаге, `None` если
     user-шаг с этим `message_step_id` не найден → caller `404 message_not_found`.
   - Образец каскадного DELETE с скоупом по владельцу — `chats/repository.delete_session`.
3. **Точка вызова** (`src/app/chat/orchestrator.py`, `run()`): после резолва сессии
   (`get_or_create_session`) и **до** `add_step(role="user", ...)` нового хода: если
   `edit_message_step_id is not None` → вызвать
   `await self._deps.repo.truncate_from_message_step(sess.id, edit_message_step_id)`; если вернул
   `None` (user-шаг не найден) → `raise MessageNotFoundError(...)`. Усечение в той же сессии
   (`sess.id` уже проверена на владельца через resume). Новый `message_step_id` (uuid4) генерируется
   как сейчас в начале `run()` — он отличается от усечённого, поэтому новый дебит по нему пройдёт.
   - **404 отдаётся через выделенный `MessageNotFoundError`** (`src/app/errors.py`,
     `MessageNotFoundError(NotFoundError)` с `code="message_not_found"` — паттерн
     `WorkspaceNotFoundError`/`SessionNotFoundError`). Handler сериализует `exc.code` → клиент
     получает `404` с машиночитаемым `message_not_found`. **Голый `NotFoundError("message_not_found")`
     недопустим** — его wire-код был бы дефолтным `not_found`, что нарушило бы нормативный wire-контракт
     `02-api-contracts.md` (требуется machine-readable `message_not_found`). Это причина, по которой
     введён отдельный класс ошибки.
   - **Edit требует resume:** если `edit_message_step_id is not None` и сессия оказалась новой
     (`ctx.is_new == True` — sessionId был, но сессия чужая/истёкшая/отсутствует), это эквивалент
     «нет хода для редактирования» → `raise MessageNotFoundError(...)` **до** усечения и **до**
     `add_step` (новосозданная пустая сессия откатывается транзакцией запроса). Практически
     `is_new` при edit означает невозможность найти ход → `404 message_not_found`.
4. **Роутер** (`src/app/api_gateway/routers/chat.py`, `chat_run`): прокинуть
   `edit_message_step_id=body.editMessageStepId` в `orchestrator.run(...)`. Сигнатуру
   `ChatOrchestrator.run` расширить параметром `edit_message_step_id: uuid.UUID | None = None`.
5. **Миграции НЕТ.** Только код. Существующие колонки/индексы (`ix_steps_session_seq`,
   `ix_steps_message_step`) покрывают запросы усечения.
6. **Код НЕ писать в рамках этого ADR** (это задача backend-агента). ADR фиксирует контракт и
   точные указания.
