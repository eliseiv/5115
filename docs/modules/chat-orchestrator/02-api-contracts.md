# Chat Orchestrator — API Contracts

## POST /v1/chat/run
Старт или продолжение агентного шага.

### Request
```json
{
  "userId": "uuid",
  "projectId": "string (optional)",
  "sessionId": "uuid (optional)",
  "message": "string (optional, если есть ≥1 attachment — ADR-039)",
  "mode": "credits | byok",
  "assistantMode": "chat | code (optional)",
  "model": "string (optional)",
  "workspaceProjectId": "uuid (optional)",
  "attachments": [
    {
      "type": "image | document | text",
      "mediaType": "image/png",
      "filename": "photo.png (optional)",
      "data": "<base64>"
    }
  ],
  "context": { "codeLanguage": "Swift", "responseStyle": "concise (optional)" },
  "editMessageStepId": "uuid (optional, редактирование сообщения — ADR-040)",
  "dialogMode": "smart | deep_thinking | study_learn | search (optional, session-fixed — ADR-055)",
  "temporary": "bool (optional, default false — временный чат, ADR-056)",
  "history": [ { "role": "user | assistant", "content": "string" } ]
}
```
- `sessionId` отсутствует → создаётся новая сессия. На сессию фиксируются: `mode` (billing_mode, credits|byok — **способ оплаты**, [ADR-012](../../adr/ADR-012-assistant-mode-vs-billing-mode.md)), `assistantMode` (тип ассистента chat|code), `dialogMode` (режим диалога, опц., см. ниже), `model` (опц., см. ниже), `projectId` (опц., см. ниже) и `workspaceProjectId` (привязка к рабочему пространству, [ADR-013](../../adr/ADR-013-workspace-projects-vs-website-builder.md)).
<a id="dialogmode-adr-055"></a>
- **`dialogMode` (опц., session-fixed, [ADR-055](../../adr/ADR-055-dialog-mode.md)).** Режим обработки хода: `smart` (базовый) / `deep_thinking` (reasoning) / `study_learn` (квиз, [ADR-057](../../adr/ADR-057-study-learn-quiz-contract.md)) / `search` (веб-поиск, [ADR-059](../../adr/ADR-059-openai-default-provider.md)). **Ортогонален** `mode` (billing) и `assistantMode` (chat/code) — третье независимое поле, не расширение `assistant_mode` ([ADR-055 §1](../../adr/ADR-055-dialog-mode.md)). Фиксируется на сессию при создании:
  - **без `dialogMode`** → резолв `request → user_preferences.default_dialog_mode → smart` (копия механики `resolved_assistant_mode`); `chat_sessions.dialog_mode = 'smart'` по умолчанию.
  - **Resume:** берётся `chat_sessions.dialog_mode`; поле запроса игнорируется (не ошибка) — как `model`/`assistantMode`/`projectId`.
  - **Provider-gate:** `deep_thinking`/`study_learn`/`search` требуют активного OpenAI ([ADR-059](../../adr/ADR-059-openai-default-provider.md)); иначе при создании сессии → **`422 unsupported_dialog_mode`** (`UnsupportedDialogModeError`, по образцу `unsupported_model` [ADR-034 §3](../../adr/ADR-034-user-model-selection.md)). Это `422`, а не `blocked` — неверный запрос, не бизнес-блок ([ADR-004](../../adr/ADR-004-blocked-http-200.md)); не в `blockReason`/`reasons[]`. `smart` работает на обоих провайдерах.
  - **Влияние на генерацию:** `deep_thinking` → `effective_model = DEEP_THINKING_MODEL` (переопределяет `sess.model` и stale-guard) + `reasoning={"effort": ...}`, реплей `reasoning.encrypted_content`; `search` → `{"type":"web_search"}` рядом с function-tools, цитаты `url_citation` в `content_blocks`/`serverTools`; `study_learn` → инструмент `quiz.generate` + обучающий суффикс промта. Режимный суффикс промта — **после** base assistant_mode и workspace-инструкций.
  - Биллинг неизменен (1 кредит, [ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md)). `ChatRunRequest.dialogMode: str|None` (не `Literal` — иначе generic 422 без машинного кода).
<a id="temporary-adr-056"></a>
- **`temporary` / `history` (опц., временный чат, [ADR-056](../../adr/ADR-056-temporary-chat.md)).** При `temporary=true` ход **не персистится** (ни `chat_sessions`/`chat_steps`/`tool_calls`) — `repo` подменяется на in-memory `EphemeralRepository` с сигнатурами `ChatRepository` (инвариант единственного писателя [ADR-021](../../adr/ADR-021-deterministic-step-order-and-block-normalization.md) сохранён). Историю присылает клиент полем `history` (провайдер-агностичный текстовый транскрипт `{role, content}`, только user/assistant; сырые `tool_use` без пары дали бы `400`/BUG-5).
  - **Континьюация через `/chat/tool-result` НЕДОСТУПНА** во временном чате → **client-side tools отключены** (`include_client_side=False`): им нужна континьюация, восстанавливающая ход из БД ([ADR-025](../../adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)). **Server-side** инструменты (`time.now`/`quiz.generate`/`image.generate`) разрешены — исполняются в одном запросе ([ADR-011](../../adr/ADR-011-server-side-tools.md)). Client-side `tool_use` при отключённом наборе (аномалия) → `UpstreamError` (симметрия `site.*`-guard).
  - **Взаимоисключения → `422`:** `temporary` + `sessionId` (нельзя резюмировать) / `editMessageStepId` / `projectId` / `workspaceProjectId` (завязаны на персист); `history` без `temporary`; `len(history) ≤ size_limit_context`.
  - **Валидация `history` (первая реплика — от user):** если `history` непуста, её **первая** реплика обязана иметь `role == "user"`, иначе → **`422`**. Причина: Anthropic жёстко требует, чтобы первое сообщение хода было от user; без нашей проверки провайдер вернул бы `400` → непрозрачный `502` вместо честного `422`. **Строгое чередование ролей НЕ требуется** — подряд идущие реплики одной роли допустимы (оба провайдера принимают; Anthropic объединяет соседние сообщения одной роли). В транскрипт пускаются только текстовые user/assistant реплики (без `tool_use`/`tool_result`).
  - **Ответ:** `stepId = null` (persisted-шага нет), `messageStepId` **присутствует** (ключ идемпотентности дебета, минтится на ход [ADR-005](../../adr/ADR-005-idempotency-ledger.md)). `audit`/`wallet.consume` с `session_id=None` (`_validate_session` уже пропускается при `None`). **Кредит списывается** (временный ≠ бесплатный, [ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md)); `store=False` в Responses. **Trial:** первый ход без подписки потребляет пожизненный trial (`users.trial_used`), `EphemeralRepository` не переопределяет `mark_trial_used` ([ADR-056 §4](../../adr/ADR-056-temporary-chat.md)) — иначе `temporary` был бы бесплатным обходом лимита. **Следы в БД (три, без текста диалога):** переписка не сохраняется, но остаются не-контентные `ledger_transactions` (дебит; привязан к `user_id`+`idempotency_key`=`messageStepId`, **колонки `session_id` у таблицы нет**), `audit_logs` (`billing_debit`, `session_id=NULL` — здесь и обеспечивается инвариант «нет FK на несуществующую сессию») и флаг `users.trial_used`. Идемпотентность дебета — только в пределах запроса ([TD-032](../../100-known-tech-debt.md), by design). «Не обучается AI» — гарантия договора провайдера API + `store=False`, **не** флаг запроса ([ADR-056 §5](../../adr/ADR-056-temporary-chat.md)).
  - **Синтетический `sessionId` временного хода (нерезюмируемость).** Ответ временного хода несёт `sessionId` синтетической (непersisted) сессии. Он **не** резолвится в БД: повторная отправка его в `POST /v1/chat/run` **без** `temporary` → resume **невозможен**, `sessionId` игнорируется как несуществующий и создаётся **новая персистентная сессия** (единообразно с обычным поведением «неизвестный sessionId → новая сессия»); в `POST /v1/chat/tool-result` → **`404`** (сессия/tool_call не найдены в БД). Временный чат по построению одноразов — держать «сессию» на клиенте через `history`, не через `sessionId`.
<a id="model-опц-session-fixed-adr-034"></a>
- **`model` (опц., session-fixed, [ADR-034](../../adr/ADR-034-user-model-selection.md)).** Выбор модели из разрешённого инстансом набора (`GET /v1/models`). Фиксируется на сессию при создании (как `mode`/`assistantMode`/`projectId`):
  - **без `model`** → сессия создаётся с `chat_sessions.model = NULL` = «дефолтная модель инстанса» (`ANTHROPIC_MODEL`/`OPENAI_MODEL` активного провайдера) — обратная совместимость;
  - **с `model`** → должен быть непустой строкой после `strip` (пустая/whitespace → `422 validation_error`) **и** входить в allowlist активного провайдера (`GET /v1/models`); иначе → **`422` с машинным кодом `unsupported_model`** (`error.code = "unsupported_model"`, сообщение `"model '<x>' is not available on this instance"`). Тихого фолбэка на дефолт нет — явный контракт ([ADR-034 §3](../../adr/ADR-034-user-model-selection.md) + ревизия 2026-07-09).
  - **⚠️ ТЗ backend (docs↔code fix, [ADR-034 §3 ревизия](../../adr/ADR-034-user-model-selection.md), [INDEX §Ревизии](../../adr/INDEX.md#ревизии)):** на данный момент код бросает **generic `ValidationFailedError` (`code="validation_error"`)** — именованного кода `unsupported_model` в wire НЕТ. Требуется: ввести `UnsupportedModelError(ValidationFailedError)` с `code="unsupported_model"` в `src/app/errors.py` (по образцу `UnsupportedDialogModeError`) и бросать его в model-гейте `orchestrator.py` (allowlist-проверка `model NOT IN allowed_models()`) вместо generic `ValidationFailedError`. Сообщение и HTTP-статус (422) сохранить; обработчик `AppError` сериализует `exc.code` → `{"error":{"code":"unsupported_model"}}`. Обновить затронутые тесты. Цель — симметрия с `unsupported_dialog_mode` (структурно идентичный instance-capability-гейт): iOS различает «модель недоступна на инстансе» vs «тело невалидно» по машинному коду. Аддитивно (422 не меняется). Field-описание `ChatRunRequest.model` в `schemas/chat.py` уже упоминает `unsupported_model` — станет точным после фикса.
  - **Resume-сессия:** `model` берётся из сессии (`chat_sessions.model`); поле запроса при resume **игнорируется** (не ошибка) — единообразно с `mode`/`assistantMode`/`projectId`. Валидация модели выполняется **только при создании** (на resume сохранённая модель уже валидна).
  - **Биллинг от выбора модели не зависит** (1 кредит = 1 сообщение, [ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md)). Возвращаемый `usage.model` отражает фактически использованную модель.
  - Инстанс одно-провайдерный ([ADR-033](../../adr/ADR-033-llm-provider-abstraction.md)) → allowlist = модели активного провайдера; выбрать чужую (Claude на openai-инстансе) нельзя.
- **`projectId` (опц., [ADR-022](../../adr/ADR-022-optional-project-and-tool-gating.md)).** Основной поток сервиса — **чат-агрегатор**; website-builder — **опциональная** фича. Поле фиксируется на сессию при создании (как `mode`/`assistantMode`):
  - **без `projectId`** → «чистый чат»: сессия создаётся с `project_id = NULL`; server-side `site.*` tools **НЕ предлагаются** Claude (нет проекта для записи); прочие client-side tools (`files.*`/`calendar.*`/`reminders.*`) доступны по обычным правилам;
  - **с `projectId`** → website-builder доступен: `site.*` входят в tool-набор, как сейчас.
  - **Resume-сессия:** `projectId` берётся из сессии (`chat_sessions.project_id`); поле запроса при resume **игнорируется** (не ошибка) — единообразно с `mode`/`assistantMode` ([ADR-022 §4](../../adr/ADR-022-optional-project-and-tool-gating.md)). Гейтинг tools — [03-architecture.md §Гейтинг tools](03-architecture.md#гейтинг-site-tools-по-наличию-проекта-adr-022). Биллинг/policy от наличия `projectId` **не зависят** (1 кредит = 1 сообщение).
- **`mode` vs `assistantMode` ([ADR-012](../../adr/ADR-012-assistant-mode-vs-billing-mode.md)):** `mode` = `billing_mode` (оплата, без изменений — обратная совместимость). `assistantMode` = тип ассистента (chat|code), **новое опциональное** поле. При отсутствии → `user_preferences.default_assistant_mode` (модуль [preferences](../preferences/README.md)), при отсутствии preferences → `chat`. `assistantMode` влияет на base-system-prompt и состав tool-реестра ([Q-012-1](../../99-open-questions.md)), **НЕ** на policy/billing.
<a id="workspaceprojectid-adr-036"></a>
- **`workspaceProjectId` (опц., uuid, session-fixed, [ADR-013](../../adr/ADR-013-workspace-projects-vs-website-builder.md)/[ADR-036](../../adr/ADR-036-workspaces-implementation.md)).** Привязка чата к рабочему пространству. Фиксируется на сессию при создании (как `mode`/`assistantMode`/`model`/`projectId`):
  - **без `workspaceProjectId`** → сессия создаётся с `chat_sessions.workspace_project_id = NULL` (чат без workspace) — обратная совместимость;
  - **с `workspaceProjectId`** → валидируется **принадлежность workspace пользователю** (`sub`); чужой/несуществующий → **`404 workspace_not_found`** (изоляция, не раскрывать чужое существование). При создании: `workspace.instructions` подмешиваются в system-prompt **после** base assistant_mode prompt ([ADR-012](../../adr/ADR-012-assistant-mode-vs-billing-mode.md)/[ADR-036 §3](../../adr/ADR-036-workspaces-implementation.md)); файлы-знания workspace подаются как контекст первого хода (document/text → `extracted_text`, image → vision; [ADR-036 §6](../../adr/ADR-036-workspaces-implementation.md));
  - **`instructions` — на КАЖДОМ ходе сессии с workspace (turn 0, resume И continuation), файлы — только turn 0 ([ADR-036 §3](../../adr/ADR-036-workspaces-implementation.md), [ADR-038 §3](../../adr/ADR-038-move-chat-to-workspace.md)).** `instructions` живут в параметре `system` (не в истории сообщений), поэтому переинъектируются в system-prompt на **каждом** обращении к LLM при наличии у сессии `workspace_project_id` — **независимо от `ctx.is_new`** (turn 0, resume/следующее сообщение, continuation-витки `/chat/tool-result`), helper `_system_prompt_with_workspace`. На turn 0 instructions берутся из `context_for_session` (instructions + файлы), на resume/continuation — лёгким single-column чтением `instructions_for_session` (только instructions). Без развязки от `is_new` перенесённый чат ([ADR-038](../../adr/ADR-038-move-chat-to-workspace.md)) не получал бы инструкции проекта на следующих ходах. Файлы-знания (`extracted_text`/vision) подаются один раз на turn 0 — сохраняются как content-блоки истории и реплеятся автоматически; на resume/continuation повторно **не** подаются (turn-0-only; обоснование стоимости/кэша — [ADR-038 §3.2](../../adr/ADR-038-move-chat-to-workspace.md)).
  - **Resume-сессия:** `workspaceProjectId` берётся из сессии; поле запроса при resume **игнорируется** (не ошибка). Файлы заново не инжектируются (turn-0-only); `instructions` подаются в `system` на каждом ходе через тот же helper (включая чаты, **перенесённые** в workspace позже через `PATCH /v1/chats/{id}`, [ADR-038](../../adr/ADR-038-move-chat-to-workspace.md)).
  - **Изменение привязки существующего чата ([ADR-038](../../adr/ADR-038-move-chat-to-workspace.md)):** `workspaceProjectId` в `/chat/run` остаётся **session-fixed**. Перенести/сменить/убрать привязку у существующей сессии — через `PATCH /v1/chats/{id}` с полем `workspaceProjectId: uuid|null` ([chats/02-api-contracts.md](../chats/02-api-contracts.md#patch-v1chatsid)). `/chat/run` каналом смены привязки не является.
  - **Не путать** с `projectId` (website-builder, TEXT) — разные поля, разная семантика ([ADR-013](../../adr/ADR-013-workspace-projects-vs-website-builder.md)). Биллинг неизменен (1 кредит, [ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md)).
<a id="message-adr-039"></a>
- **`message` (опц. при наличии вложений, [ADR-039](../../adr/ADR-039-optional-message-with-attachments.md)).** Текст сообщения пользователя. Ранее обязателен (`min_length=1`); теперь **опционален**, тип `str` с дефолтом `""`. Правило валидности хода: **`message` непуст после `strip` ИЛИ есть ≥1 элемент в `attachments` запроса**; если и текст пуст (после strip), и вложений нет → **`422`** `"message or at least one attachment is required"`. Size-лимит `message` (≤32KB) сохранён.
  - **Сборка turn-0 user-сообщения.** Text-блок добавляется в user-content **только если итоговый текст непуст** (после склейки с context-блоком [ADR-037](../../adr/ADR-037-chatrunrequest-context-allowlist-injection.md), см. [§context](#context-adr-037)). При пустом тексте отправляются **только** attachment-блоки (vision/document/text-file) — **пустой text-блок (`text=""`) не отправляется ни Anthropic, ни OpenAI** (провайдер может отвергнуть; [ADR-033](../../adr/ADR-033-llm-provider-abstraction.md), [ADR-039 §2,§4](../../adr/ADR-039-optional-message-with-attachments.md)).
  - **Склейка с context-блоком ([ADR-037](../../adr/ADR-037-chatrunrequest-context-allowlist-injection.md)):** message непуст + блок → `block + "\n\n" + message` (как раньше); message непуст, блока нет → `message`; **message пуст + блок → `block`** (без висячего `"\n\n"`, text-блок присутствует); **message пуст + блока нет → text-блока нет** (только attachment-блоки). Whitespace-only message при наличии вложения трактуется как «нет текста» (text-блок не создаётся).
  - **Edge / scope:** только текстовое файл-вложение (`type: text`/`document`) без текста — валидно. Пустой `message` + **только** workspace-файлы ([ADR-036](../../adr/ADR-036-workspaces-implementation.md)), без `attachments` запроса → **`422`** (требование «≥1 attachment» относится к `attachments` **запроса**; workspace-контекст ход «с вложением» не делает). Биллинг неизменен (1 кредит, [ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md)); миграции нет; обратная совместимость полная (непустой message без вложений — как раньше).
<a id="editmessagestepid-adr-040"></a>
- **`editMessageStepId` (опц., uuid, [ADR-040](../../adr/ADR-040-edit-message-and-regenerate.md)) — редактирование отправленного сообщения.** Когда указано — backend **усекает** историю сессии от хода `editMessageStepId` (его user-шаг и **всё, что после**) и генерирует **новый** ход с переданными `message`/`attachments`/`context`. Один атомарный вызов (усечение + новый ход в транзакции запроса), без отдельного endpoint'а.
  - **Требует `sessionId` (resume).** `editMessageStepId` **без** `sessionId` → **`422`** (`"editMessageStepId requires sessionId"`). Редактирование возможно только в существующей сессии; нельзя сочетать с созданием новой сессии.
  - **Изоляция / несуществующая сессия:** если сессия чужая / не существует / истекла (resume не выполняется) → **`404`** (нет хода для редактирования; чужой чат усечь нельзя). Усечение скоупится по уже проверенной на владельца (`sub`) сессии.
  - **Несуществующий ход:** если в сессии **нет user-шага** с `message_step_id = editMessageStepId` → **`404 message_not_found`**. Anchor хода ищется **строго по `role='user'`**; если `editMessageStepId` указывает на assistant/tool-шаг (нет user-шага) → тоже **`404 message_not_found`** (редактируется только сообщение пользователя, не ответ ассистента).
  - **Семантика усечения ([ADR-040 §2](../../adr/ADR-040-edit-message-and-regenerate.md)):** anchor = минимальный `chat_steps.seq` ([ADR-021](../../adr/ADR-021-deterministic-step-order-and-block-normalization.md)) user-шага с этим `message_step_id`; удаляются все `chat_steps` с `seq >= anchor` **и явно** `tool_calls` усечённых ходов (по их `message_step_id`). `tool_calls` удаляются **явно**, т.к. их FK завязан на `chat_sessions` (`session_id`), **не** на `chat_steps` — каскад при удалении шагов **не** срабатывает, иначе остались бы осиротевшие `tool_calls`. Усечение — **до** записи нового user-шага, в той же транзакции запроса (общий commit хода).
  - **Биллинг (refund-policy, [ADR-040 §3](../../adr/ADR-040-edit-message-and-regenerate.md)):** регенерация = обычный ход с **новым** `message_step_id` → **новый дебит 1 кредита** ([ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md), идемпотентность по `(user_id, message_step_id)` сохраняется). **Возврата за удалённый старый ход НЕТ** (no-refund-on-edit): кредит за уже сгенерированный (ныне усечённый) ход потреблён. Пересмотр → [Q-040-2](../../99-open-questions.md).
  - **Edge — редактирование ПЕРВОГО сообщения чата ([ADR-040 §4а](../../adr/ADR-040-edit-message-and-regenerate.md)):** усечение удаляет всю историю, сессия становится пустой, но **существует** → `ctx.is_new = False`. Поэтому **workspace-файлы НЕ переинъектируются** (turn-0-only, вариант a [ADR-038 §3.2](../../adr/ADR-038-move-chat-to-workspace.md)) — приемлемо и зафиксировано (симметрия с inline-attachments [ADR-020](../../adr/ADR-020-inline-base64-attachments-mvp.md)); `instructions` workspace инъектируются как обычно (на каждом ходе, развязано от `is_new`, [ADR-038 §3](../../adr/ADR-038-move-chat-to-workspace.md)). Инлайн-attachments нового хода подаются как turn-0 нового хода. Пересмотр реинъекции файлов → [Q-040-3](../../99-open-questions.md).
  - **Edge — открытый tool-loop ([ADR-040 §4б](../../adr/ADR-040-edit-message-and-regenerate.md)):** редактируемый или последующий ход с pending `tool_calls` / незакрытым барьером ([ADR-025](../../adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)) — усечение удаляет эти шаги и их `tool_calls` → **никаких осиротевших `tool_calls`/незакрытых барьеров**. «Зависший» tool_call сбрасывается редактированием.
  - **Без `editMessageStepId` `/chat/run` не меняется** (обратная совместимость полная). Миграции нет. В `/chat/tool-result` поле не применимо (редактирование — только новый ход `/chat/run`).
- `attachments[]` (опц., ≤ `ATTACHMENT_MAX_COUNT`, дефолт 10) — **inline base64-вложения** ([ADR-020](../../adr/ADR-020-inline-base64-attachments-mvp.md), заменяет двухшаговую модель [ADR-014](../../adr/ADR-014-multimodal-attachments.md)). Принимаются **только** в первом (новом) пользовательском message-шаге `/chat/run`; в `/chat/tool-result` — **не** принимаются. Поля вложения:
  - `type` ∈ `image | document | text` — класс вложения.
  - `mediaType` — конкретный MIME, строго из allowlist (см. ниже); вне allowlist → `422 unsupported_media_type`.
  - `filename` (опц.) — для человекочитаемой разметки (особенно `text`-вложений).
  - `data` — base64-кодированное содержимое (валидный base64; невалидный → `422`).
  - **Маппинг (провайдер-aware, [ADR-033 §5](../../adr/ADR-033-llm-provider-abstraction.md)):**
    - **Anthropic:** `image` → `{"type":"image","source":{"type":"base64",...}}`; `document` (PDF) → нативный `{"type":"document","source":{"type":"base64","media_type":"application/pdf",...}}`; `text` → `{"type":"text","text":"<filename>\n```\n<UTF-8 текст>\n```"}`.
    - **OpenAI:** `image` → `{"type":"image_url","image_url":{"url":"data:<mediaType>;base64,<data>"}}`; `text` → text-блок; `document` (PDF) → content-часть `file` (`{"type":"file","file":{"filename","file_data":"data:application/pdf;base64,..."}}`) либо извлечённый `pypdf`-текст как text-блок (фолбэк) — **PDF поддержан** ([ADR-041](../../adr/ADR-041-openai-native-pdf-attachment.md), закрывает [TD-023](../../100-known-tech-debt.md)).
  - **Allowlist `mediaType`:** `image` — `image/jpeg`, `image/png`, `image/gif`, `image/webp`; `document` — `application/pdf`; `text` — `text/plain`, `text/markdown`, `text/csv`, `application/json` ([Q-020-1](../../99-open-questions.md) — расширение).
  - **Валидация (фокус ревью, [05-security.md](../../05-security.md)):** соответствие `type`/`mediaType` реальному содержимому по magic bytes; лимиты проверяются **до** декодирования base64; PDF — guard числа страниц (анти-bomb). URL-вложения запрещены (нет backend-fetch).
  - **Реплей/хранение ([ADR-020 §3](../../adr/ADR-020-inline-base64-attachments-mvp.md)):** на первом витке полные content-блоки отправляются Claude; в `chat_steps.payload` сохраняется **лёгкий текстовый плейсхолдер** (НЕ base64); на последующих tool-витках реплеится только плейсхолдер (тяжёлый контент не повторяется).
  - **Биллинг:** обычный chat-шаг (1 кредит, [ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md) без изменений); vision/PDF-токены входят в message-шаг, отдельной тарификации нет.
<a id="context-adr-037"></a>
- **`context` (опц., object, per-message, [ADR-037](../../adr/ADR-037-chatrunrequest-context-allowlist-injection.md)).** Доп-настройки **текущего хода** (не сессии). В отличие от session-fixed `mode`/`assistantMode`/`model`/`projectId`/`workspaceProjectId`, `context` присылается и применяется на **каждом** `/chat/run` и может меняться по ходу чата. **Не** хранится в `chat_sessions`, **миграции БД нет** — влияет на содержимое текущего user-сообщения, которое персистится как user-step (`chat_steps.payload`) → корректный replay.
  - **Allowlist ключей** (всё остальное игнорируется; значения нормализуются `strip`, пустое после strip → ключ игнорируется):

    | Ключ | Тип | Валидация |
    |---|---|---|
    | `codeLanguage` | str | непустой, ≤40 символов (свободная строка; язык программирования для code-режима) |
    | `responseStyle` | str enum | `concise` \| `balanced` \| `detailed` (lower-case); вне набора → ключ игнорируется |
    | `verbosity` | str enum | `low` \| `medium` \| `high` (lower-case); вне набора → ключ игнорируется |
    | `tone` | str | непустой, ≤40 символов (свободная строка) |
    | `locale` | str | непустой, ≤35 символов, символы `[A-Za-z0-9_-]` (BCP-47-подобный); вне класса → ключ игнорируется |

  - **Поведение на невалидное (lenient).** Неизвестные ключи — **игнорируются** (forward-compat). Ключ с неверным типом/длиной/вне-enum/вне-символьного-класса значением — **этот ключ игнорируется**, остальные применяются; запрос **не** падает. Существующая size-валидация сохраняется: сериализованный `context` > `size_limit_context` (≤64KB) → **`422`** (грубо-битое/огромное тело); не-объект → `422` (StrictModel).
  - **Куда инъектируется.** Backend собирает детерминированный компактный текст-блок (фикс. порядок ключей `codeLanguage, responseStyle, verbosity, tone, locale`, экранирование разделителей), напр. `[Conversation settings for this message: codeLanguage=Swift; responseStyle=concise; locale=ru-RU]`, и добавляет его к содержимому **user-сообщения turn0**: блок **лидирует**, затем `\n\n`, затем `message`. **НЕ в system-prompt** (prompt-кэш не ломается; нет повышения авторитета пользовательских данных, [05-security.md](../../05-security.md)). На continuation/`/chat/tool-result` повторно **не** подаётся (уже в истории хода).
  - **Кэш-инвариант / провайдер-агностичность.** `system`+`tools` от `context` не зависят → prompt-кэш Anthropic не инвалидируется; блок — обычный текст в user-content → одинаково на Anthropic и OpenAI ([ADR-033](../../adr/ADR-033-llm-provider-abstraction.md)).
  - **Не виден в истории/превью ([ADR-042](../../adr/ADR-042-hide-context-block-from-user-facing-history.md)).** Блок персистится внутри текста user-шага (для replay), но **в user-facing выводе скрыт**: при отдаче истории `GET /v1/chats/{id}` и превью `GET /v1/chats` ведущий блок `[Conversation settings for this message: …]` срезается (read-time strip, единый helper). Хранение `chat_steps.payload` и реплей модели (`_build_messages`) **не меняются** — модель по-прежнему получает блок. См. [chats/02-api-contracts.md §GET /v1/chats/{id}](../chats/02-api-contracts.md#get-v1chatsid).
  - **Обратная совместимость.** Без `context` / пустой объект / нет валидных ключей → user-сообщение = только `message` (поведение неизменно). Биллинг неизменен (1 кредит, [ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md)). Расширение allowlist → [Q-037-1](../../99-open-questions.md); связь с `preferences.code_defaults` (вне scope) → [TD-028](../../100-known-tech-debt.md).
- Size-лимиты: `message` ≤ 32KB, `context` ≤ 64KB (см. [05-security.md](../../05-security.md)). **Тело `/v1/chat/run` имеет повышенный transport-лимит** (`ATTACHMENT_REQUEST_BODY_LIMIT`, дефолт 12 MB) для inline base64-вложений — общий лимит `≤512KB` прочих роутов **не меняется**, повышение применяется только к роуту `/v1/chat/run` ([ADR-020](../../adr/ADR-020-inline-base64-attachments-mvp.md), [05-security.md](../../05-security.md)). Лимиты на вложения: одно ≤ `ATTACHMENT_MAX_BYTES_IMAGE` (дефолт 5 MB) / `ATTACHMENT_MAX_BYTES_DOCUMENT` (дефолт 8 MB), суммарно ≤ `ATTACHMENT_TOTAL_BYTES` (дефолт 10 MB).
- При старте нового пользовательского message-шага Orchestrator генерирует `messageStepId` (UUID), персистирует его в `chat_steps.message_step_id` и `tool_calls.message_step_id`. Он един для всех tool-раундов шага (включая re-entry через `/chat/tool-result`) и используется как ключ идемпотентности credits-debit ([ADR-005](../../adr/ADR-005-idempotency-ledger.md), [ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md)). `messageStepId` — внутренняя величина биллинга, не путать с gateway correlation `requestId` (`X-Request-Id`).

### Response (200)
```json
{
  "status": "assistant_message | tool_call | blocked",
  "sessionId": "uuid",
  "messageStepId": "uuid | null",
  "stepId": "uuid | null",
  "assistantMessage": "string (optional, при assistant_message; ТАКЖЕ при tool_call, если Claude выдал текст вместе с tool_use — ADR-024 п.3 / Q-024-1)",
  "toolCall": { "id": "uuid", "name": "string", "args": { } },
  "toolCalls": [ { "id": "uuid", "name": "string", "args": { } } ],
  "serverTools": [ { "toolCallId": "uuid", "toolName": "string (dot)", "status": "completed | errored", "summary": "string | null" } ],
  "blockReason": "enum (optional, при blocked)",
  "usage": { "inputTokens": 0, "outputTokens": 0, "model": "string" },
  "quiz": { "question": "string", "options": ["..."], "correctIndex": 0, "explanation": "string" },
  "images": [ { "imageId": "uuid", "contentType": "image/png", "size": 0 } ]
}
```
- **`toolCalls[]` (множественный, [ADR-025](../../adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)) присутствует только при `status=tool_call`** — **ВСЕ** client-side tool-вызовы текущего assistant-хода (parallel tool use), в порядке блоков ответа Claude. Каждый элемент `{ id (доменный UUID = tool_calls.id), name (dot), args }`. **Server-side `site.*` в `toolCalls[]` НЕ попадают** (исполняются на бэке в tool-loop, [ADR-011](../../adr/ADR-011-server-side-tools.md)) — массив несёт только client-side (`files.*`/`calendar.*`/`reminders.*`).
- **`toolCall` (одиночный) — deprecated, обратная совместимость ([ADR-025](../../adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)).** Присутствует при `status=tool_call` и **равен `toolCalls[0]`** (первый client-side вызов хода). Корректный клиент обязан читать `toolCalls[]` (на мульти-tool ходе одиночный `toolCall` неполон → continuation сломается). Удаление одиночного поля — отдельным ADR после миграции iOS.
- `toolCall.id` / `toolCalls[].id` — **доменный UUID** (`= tool_calls.id`), стабильный публичный идентификатор для iOS и для последующего `/chat/tool-result`. Внутренний Anthropic `tool_use.id` (`toolu_...`) наружу **не** отдаётся (хранится в `tool_calls.provider_tool_use_id`, [ADR-008](../../adr/ADR-008-provider-tool-use-id.md)).
- **`serverTools[]` — выполненные server-side инструменты за этот вызов ([ADR-028](../../adr/ADR-028-projectid-in-chat-list-and-server-tools-in-chat-response.md); поле `toolCallId` — [ADR-030](../../adr/ADR-030-toolcallid-in-server-tools.md); аддитивно):** список server-side инструментов (`site.*` project-scoped [ADR-011](../../adr/ADR-011-server-side-tools.md), `time.now` global [ADR-026](../../adr/ADR-026-global-server-side-tools-and-time-now.md)), которые backend исполнил в tool-loop **этого** вызова (`/chat/run` или один `/chat/tool-result`-continuation), в порядке выполнения. **Дополняет** `toolCalls[]` (там — только client-side, исполняемые iOS): server-side в `toolCalls[]` по-прежнему **НЕ** входят. Каждый элемент: `{ toolCallId, toolName, status, summary? }`:
  - `toolCallId` ([ADR-030](../../adr/ADR-030-toolcallid-in-server-tools.md), аддитивно) — **доменный** `tool_call.id` (uuid4 = `tool_calls.id`) этого server-side выполнения, **обязательное** поле (первым в элементе). **Совпадает** с `toolCallId` соответствующего tool-шага истории `GET /v1/chats/{id}` → `steps[].payload.toolCallId` ([ADR-024](../../adr/ADR-024-history-payload-domain-normalization.md)) — нормативный инвариант корреляции: `serverTools[i].toolCallId` адресует ровно один tool-шаг истории (детерминированно даже при повторных вызовах одного инструмента за ход). Это **тот же домен id**, что у client-side `toolCalls[].id` (симметрия client/server tool-id); **НЕ** provider `toolu_...` ([ADR-008](../../adr/ADR-008-provider-tool-use-id.md)). Берётся из уже доступного backend `tool_call_id` (минтится до исполнения в tool-loop).
  - `toolName` — доменное имя с точкой (`time.now`, `site.write_file`, …), совпадает с `/v1/tools` `name` и `GET /v1/chats/{id}/steps` `toolName`.
  - `status` — `"completed"` | `"errored"` (итог выполнения; `errored` — инструмент вернул tool-result error, ход при этом **не падает**). Совпадает со статусом `tool_calls`, выставляемым в `_execute_server_side_tool`/`_execute_global_server_side_tool`.
  - `summary` (опц., `string | null`) — **компактный** человекочитаемый итог, лимит длины `_SUMMARY_MAX_CHARS` (120, как в steps-view). **НЕ raw result.** Для `completed` — дефолт `"ok"` или короткий доменный итог (например имя файла) **без путей/URL/signed-token**; для `errored` — короткий код ошибки (например `invalid_timezone`). **Полный** результат server-side инструмента доступен только в истории `GET /v1/chats/{id}` → `steps[].payload` tool-шага ([ADR-024](../../adr/ADR-024-history-payload-domain-normalization.md)) и steps-view — `serverTools[]` это **индикатор**, не канал доставки результата.
  - **Семантика «за один вызов» (не за сессию):** перечисляет server-side, выполненные в этом обращении. Дубликаты с историей `/chats` — ожидаемы (удобство флоу, не замена истории).
  - **Присутствие по статусам:** при `status=assistant_message` и `status=tool_call` — **присутствует** (может быть пустым `[]`, если server-side не выполнялись; при `tool_call` перечисляет server-side, отработавшие **до** того, как ход уперся в client-side вызов). При `status=blocked`+**policy** (`blockReason ≠ max_tokens`) — **пустой `[]`** (policy-block до генерации, tool-loop не запускался). При `status=blocked`+**`max_tokens`** ([ADR-025](../../adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)) — **может быть НЕ пустым** (server-side раунды могли отработать до обрыва финального витка). Поле присутствует всегда (хотя бы как `[]`) при `assistant_message`/`tool_call`/`blocked`.
  - **Idempotent replay → `serverTools=[]` (by-design, [ADR-028](../../adr/ADR-028-projectid-in-chat-list-and-server-tools-in-chat-response.md)):** повторный `/chat/tool-result` для **уже закрытого** хода возвращает сохранённый финальный шаг (`_render_saved_step`, continuation выполняется один раз на закрытие барьера — [ADR-005](../../adr/ADR-005-idempotency-ledger.md)/[ADR-025](../../adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)); при таком реплее `serverTools=[]` — server-side выполнения **НЕ** реконструируются (реплей отдаёт финальный результат, не воспроизводит tool-loop). Полный набор server-side выполнений хода доступен в истории `GET /v1/chats/{id}`.
  - **Биллинг неизменен ([ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md)):** server-side раунды не списывают кредиты; `serverTools[]` информационно, на amount не влияет. Аддитивно/обратносовместимо: старые клиенты игнорируют. Каталог инструментов и их число (14) не меняются.
  - **Связь со steps-view:** идея `summary` переиспользована из `StepsViewStepSchema`, но это **отдельное** поле — только server-side выполнения, `status` (`completed`/`errored`) вместо `kind`. steps-view (`GET /v1/chats/{id}/steps`) — отдельный диагностический срез истории; `serverTools[]` — inline-индикатор в самом ответе генерации.
- **Контракт Anthropic tool-loop ([ADR-025](../../adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)):** на КАЖДЫЙ `tool_use` ассистент-хода в следующем витке обязан быть `tool_result`. Поэтому клиент обязан исполнить и вернуть результаты на **все** `toolCalls[]` (см. `/chat/tool-result` батч) — иначе continuation не соберётся (Anthropic `400` → `502`). Одиночный `toolCall` достаточен только когда `len(toolCalls)==1`.
- `blockReason` присутствует только при `status=blocked`.
- `usage` присутствует при `assistant_message`/`tool_call`, **а также при `blocked` с `blockReason=max_tokens`** ([ADR-025](../../adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)); при policy-blocked (генерация не выполнялась) — отсутствует.
- **`quiz` (опц., [ADR-057](../../adr/ADR-057-study-learn-quiz-contract.md)):** присутствует только когда `dialogMode=study_learn` и модель вызвала global server-side tool `quiz.generate`; иначе `null`/опущено. `QuizSchema(StrictModel)`: `question`, `options` (2..10, cap на длину), `correctIndex` (валидатор `correctIndex < len(options)`), `explanation`. Результат инструмента аккумулируется в `_generate_loop` → `ChatRunOut.quiz`. Проверка ответа — на клиенте (`correctIndex` уходит клиенту, осознанный компромисс). Совместим с временным чатом. **`quiz` в ответе всегда валиден** (или `null`): невалидные аргументы квиза от модели **не** роняют ход `422` — backend возвращает `tool_result`-ошибку, модель **исправляет квиз в том же ходе** (graceful degrade, как `time.now`/`invalid_timezone` [ADR-026 §6](../../adr/ADR-026-global-server-side-tools-and-time-now.md)); покрывает **все** нарушения серверной валидации аргументов (`correctIndex`-диапазон, длины, число вариантов), а не только `correctIndex` ([ADR-057 §3](../../adr/ADR-057-study-learn-quiz-contract.md), ревизия 2026-07-09). При упорстве модели до `MAX_SERVER_TOOL_ROUNDS` — существующий guard ([ADR-011 §2](../../adr/ADR-011-server-side-tools.md)): `UpstreamError → 502`, без биллинга (патологично). **Strict-нюанс:** OpenAI strict не принимает `minLength`/`maxLength`/`minItems`/`maxItems`/…, поэтому wire-схема очищается, а ограничения держатся серверной Pydantic-валидацией + текстом описания (не добавлять эти ключи в strict-схему — провайдер даст `400`).
- **`images` (опц., [ADR-058](../../adr/ADR-058-image-generation.md)):** список изображений, сгенерированных server-side инструментом `image.generate` (OpenAI `gpt-image-1`) в этом ходе. Каждый элемент — **строго `{ imageId, contentType, size }`** (без `prompt` и без байтов, приватность/[TD-035](../../100-known-tech-debt.md), [ADR-058 §7](../../adr/ADR-058-image-generation.md)); байты фетчатся `GET /v1/images/{imageId}` под JWT (чужое/истёкшее → `404`). Каждое изображение = **отдельный дебет `IMAGE_CREDITS_COST`**, списывается **независимо от `mode` — и при `credits`, и при `byok`** (изображение идёт через наш серверный `OPENAI_API_KEY`; BYOK покрывает только текст, [ADR-058 §4](../../adr/ADR-058-image-generation.md)); итог `credits` → `1 + N×COST`, `byok` → `N×COST`; идемпотентно по `tool_call_id`. Нехватка кредитов на картинку (в любом режиме, включая trial) → `status=blocked` + `blockReason=image_credits_empty`, байты **не** сохранены (порядок `generate`→`consume`→`INSERT`, rollback; байты уже сгенерированы на нашем ключе, но не отданы — [ADR-058 §5](../../adr/ADR-058-image-generation.md)). **Ошибки генератора деградируют в tool_result-ошибку (ход не падает, не 502):** content-policy (`content_policy`, модель правит промт в том же ходе) и сбой генерации/невалидный ключ (`image_generation_failed`, логируется WARNING); image-дебет при сбое **не** списывается ([ADR-058 §3](../../adr/ADR-058-image-generation.md)). **Активен, если задан `OPENAI_API_KEY`** (генератор независим от `LLM_PROVIDER` — Anthropic-инстанс с ключом тоже генерирует; [ADR-058 §3](../../adr/ADR-058-image-generation.md)).
- **`status=blocked` + `blockReason=max_tokens` (обрезка по лимиту output-токенов, [ADR-025](../../adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)):** Claude обрезан на `ANTHROPIC_MAX_TOKENS` (`stop_reason="max_tokens"`); обрезанные `tool_use` **неполны** и наружу **НЕ** отдаются (`toolCall`/`toolCalls` отсутствуют). В отличие от policy-blocked: `messageStepId`/`stepId` **НЕ null** (ход и обрезанный assistant-шаг созданы), `usage` присутствует, `assistantMessage` — частичный текст хода (если был). **Кредит НЕ списывается** (обрыв — не успешный финальный `assistant_message`, [ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md)). Клиенту рекомендуется повторить/сократить запрос. С дефолтом `ANTHROPIC_MAX_TOKENS=16000` кейс редкий (safety-net).
- **`assistantMessage` ([Q-024-1](../../99-open-questions.md) Closed = вариант A, [ADR-024 §Decision п.3](../../adr/ADR-024-history-payload-domain-normalization.md)):**
  - `status=assistant_message` — финальный текст Claude (как и раньше, без изменений).
  - `status=tool_call` — **опционально присутствует**: текст из `text`-блоков **того же** assistant-шага, чей `tool_use` вернулся как `toolCall` (тот шаг, на который указывает `stepId`). Значение = текст/конкатенация `text`-блоков этого шага. Если Claude вернул `tool_use` **без** сопутствующего текста — `assistantMessage = null`/опущено. `toolCall` при этом **обязателен** (семантика не меняется); добавление `assistantMessage` аддитивно/обратносовместимо (поле уже опционально-nullable в схеме; новизна — оно теперь может быть НЕ-null при `tool_call`). Backend перестаёт отбрасывать сопутствующий текст (`orchestrator.py:661`) и кладёт его в `assistantMessage`.
  - `status=blocked` — `assistantMessage = null` (генерация не выполнялась).
  - **Согласование с историей и [ADR-023](../../adr/ADR-023-sync-ids-in-chat-response.md):** `assistantMessage` при `tool_call` = тот же текст, что отдают `text`-блоки `GET /v1/chats/{id}` → `steps[].payload.content[]` шага `stepId` (нормализация истории текстовые блоки не меняет — байт-в-байт хранилище). Инвариант: `ChatResponse.stepId` указывает на этот же assistant-шаг, поэтому run-проекция и история несут один и тот же сопутствующий текст.
- **`messageStepId` / `stepId` — идентификаторы синхронизации с историей чата ([ADR-023](../../adr/ADR-023-sync-ids-in-chat-response.md), nullable).** Позволяют клиенту склеить ответ генерации с шагами `GET /v1/chats/{id}` → `steps[]`. Обе величины уже существуют в orchestrator: `messageStepId` = `chat_steps.message_step_id` (ключ хода, см. §below про генерацию), `stepId` = `chat_steps.id` (PK конкретного шага). Семантика по статусам:
  - `status=assistant_message`: `messageStepId` = ход; `stepId` = `id` финального assistant-шага (= `ChatStepSchema.id` этого шага в истории). **Оба присутствуют.**
  - `status=tool_call`: `messageStepId` = ход; `stepId` = `id` assistant-шага, содержащего `tool_use` (тот шаг истории, чей `payload` несёт этот `tool_use`-блок). `toolCall.id` **остаётся как есть** (provider-независимый доменный id tool-вызова для `/chat/tool-result`) — `toolCall.id` ≠ `stepId`. **Оба присутствуют.**
  - `status=blocked` (**policy-blocked**, `blockReason ≠ max_tokens`): `messageStepId = null`, `stepId = null` — блокировка срабатывает в Policy Engine **до** генерации ([ADR-002](../../adr/ADR-002-access-policy-state-machine.md), [ADR-004](../../adr/ADR-004-blocked-http-200.md)), `chat_steps`/ход **не создаются**, ссылаться не на что (согласовано с отсутствием `usage` при policy-blocked).
  - `status=blocked` + **`blockReason=max_tokens`** (обрезка, [ADR-025](../../adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)): `messageStepId` = ход, `stepId` = `id` обрезанного assistant-шага — **оба НЕ null** (Claude сгенерировал контент, ход/шаг созданы). `usage` присутствует. Отличие от policy-blocked: здесь блокировка — обрыв **после** начала генерации, а не deny до неё.
- **Инвариант синка id шага/хода (нормативно):** `ChatResponse.messageStepId` / `ChatResponse.stepId` дословно совпадают с `ChatStepSchema.messageStepId` / `ChatStepSchema.id` соответствующего шага в [chats/02-api-contracts.md `GET /v1/chats/{id}` → `steps[]`](../chats/02-api-contracts.md#get-v1chatsid). Аддитивно/обратносовместимо: существующие поля, security, коды, пути не меняются ([ADR-023](../../adr/ADR-023-sync-ids-in-chat-response.md)).
- **Инвариант синка имени/id инструмента (нормативно, [ADR-024](../../adr/ADR-024-history-payload-domain-normalization.md)):** `toolCall.name` (dot) и `toolCall.id` (domain UUID = `tool_calls.id`) этого ответа **дословно совпадают** с `tool_use.name`/`tool_use.id` соответствующего блока в `GET /v1/chats/{id}` → `steps[].payload.content[]` (история нормализует свой сырой wire-payload к доменному виду при отдаче — см. [chats/02-api-contracts.md](../chats/02-api-contracts.md#get-v1chatsid)) и с `name` в `/v1/tools`. Сопутствующий текст при `status=tool_call` (`text`-блок того же шага) в истории доступен полностью и **также** пробрасывается в `ChatResponse.assistantMessage` ([Q-024-1](../../99-open-questions.md) Closed = вариант A): тот же текст того же шага (`stepId`) — см. описание `assistantMessage` выше.

### Правила
- Перед генерацией — обязательный вызов Policy Engine (ADR-002).
- `status=blocked` → HTTP 200, машиночитаемый `blockReason` (ADR-004).
- Для `status=tool_call` payload строго типизирован по схемам ниже.
- Тех. ошибки (auth/size/validation/upstream) — 4xx/5xx (см. api-gateway).

## POST /v1/chat/tool-result
Приём результата(ов) локальных tools и продолжение шага. **Батч-форма ([ADR-025](../../adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md))** — для parallel tool use возвращаются результаты на все `toolCalls[]` хода.

### Request (батч — рекомендуемая форма, [ADR-025](../../adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md))
```json
{
  "userId": "uuid",
  "sessionId": "uuid",
  "results": [
    { "toolCallId": "uuid", "result": { "any": "object" } },
    { "toolCallId": "uuid", "error": { "code": "string", "message": "string" } }
  ]
}
```
- `results[]` — результаты на один или несколько tool-вызовов **одного хода**. В каждом элементе ровно одно из `result` / `error` (валидатор `extra=forbid` поэлементно).
- Каждый `result` ≤ 256KB (поэлементно).

### Request (одиночная форма — deprecated, обратная совместимость)
```json
{
  "userId": "uuid",
  "sessionId": "uuid",
  "toolCallId": "uuid",
  "result": { "any": "object" },
  "error": { "code": "string", "message": "string" }
}
```
- Эквивалентна `results = [{ toolCallId, result|error }]` (батч из одного). Backend принимает обе формы; одиночная — **deprecated** ([ADR-025](../../adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)), удаление — отдельным ADR после миграции iOS.
- Ровно одно из `result` / `error`.
- `result` ≤ 256KB.

### Барьер хода и continuation ([ADR-025](../../adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md))
- Continuation-виток к Anthropic выполняется **ТОЛЬКО** когда для **всех** client-side `tool_use` текущего assistant-хода собраны `tool_result` (completed/errored). Иначе orphan `tool_use` → Anthropic `400` → `502`.
- **Рекомендуемый путь** — один батч-запрос со всеми результатами хода → барьер закрывается сразу, backend делает continuation и возвращает следующий шаг.
- **Накопительный путь (поддерживается):** результаты можно слать частями (несколько `/chat/tool-result` одного хода). Пока барьер не закрыт — ответ `status=tool_call` с `toolCalls[]` = **оставшиеся** (ещё без результата) client-side вызовы хода (`toolCall` = первый из оставшихся); Anthropic не вызывается; биллинг не выполняется. Когда последний результат закрывает барьер — continuation-виток, следующий шаг.
- Server-side `site.*` результаты в `/chat/tool-result` **не присылаются** — backend их сформировал сам ([ADR-011](../../adr/ADR-011-server-side-tools.md)); барьер хода учитывает только client-side tool-вызовы.

### Response (200)
Та же схема, что у `/v1/chat/run` (включая `messageStepId` / `stepId`, [ADR-023](../../adr/ADR-023-sync-ids-in-chat-response.md), `toolCalls[]`, [ADR-025](../../adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md), и `serverTools[]`, [ADR-028](../../adr/ADR-028-projectid-in-chat-list-and-server-tools-in-chat-response.md) — server-side, выполненные в **этом** continuation-витке).
- `messageStepId` **стабилен в рамках хода**: равен тому, что был выдан в исходном `/chat/run` этого хода (берётся из `tool_calls.message_step_id` по `toolCallId`, см. re-entry ниже) — это и есть смысл синка tool-loop: клиент держит один `messageStepId` на весь ход.
- `stepId` = `id` **нового** шага, который представляет этот ответ: assistant-tool_use следующего раунда (при `status=tool_call`) либо финальный assistant-шаг (при `status=assistant_message`). Ответ всегда указывает на **следующий шаг, порождённый Claude**, а не на только что принятый шаг-`tool_result`.
- `status=blocked` (если возникает на продолжении): `messageStepId`/`stepId` = `null` — как в `/chat/run`.

### Правила
- Проверка принадлежности каждого `toolCallId` текущей сессии: `tool_calls.session_id == sessionId`, иначе `404`/`403` (применяется к каждому элементу `results[]`).
- Re-entry message-шага: `messageStepId` берётся из `tool_calls.message_step_id` найденного `toolCallId` (НЕ генерируется заново). Все элементы батча должны относиться к одному ходу (один `message_step_id`). Все ответы и финальный debit этого шага используют тот же `messageStepId`.
- **Идемпотентность / повторы ([ADR-005](../../adr/ADR-005-idempotency-ledger.md), [ADR-025](../../adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)):**
  - повторный `toolCallId` со статусом `completed`/`errored` → результат не перезаписывается, Anthropic повторно не вызывается; если барьер уже закрыт и continuation-шаг сохранён — вернуть его (как сейчас);
  - дубль `toolCallId` внутри одного батча → `422`;
  - continuation-виток к Anthropic выполняется **один раз** на закрытие барьера хода (дополнительно защищён `messageStepId`-идемпотентностью дебита, [ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md)).
- `result` валидируется по схеме соответствующего tool (см. ниже); несоответствие → `422`.

## Классы tools: client-side vs server-side ([ADR-011](../../adr/ADR-011-server-side-tools.md), [ADR-026](../../adr/ADR-026-global-server-side-tools-and-time-now.md))
Три класса инструментов ([ADR-026 §1](../../adr/ADR-026-global-server-side-tools-and-time-now.md)):
- **client-side** (`files.*`, `calendar.*`, `reminders.*`) — исполняет **iOS-клиент**: backend отдаёт `status=tool_call`,
  ждёт `tool_result` через `/v1/chat/tool-result`. Описаны в этом документе.
- **server-side, project-scoped** (`site.*`, website-builder, `SERVER_SIDE_TOOLS`) — исполняет **backend** немедленно в tool-loop, формирует `tool_result` сам
  и продолжает к Anthropic **без** round-trip к iOS; **НЕ** отдаётся клиенту как `status=tool_call`. **Требует проекта.** Схемы и поведение —
  [modules/website-builder/02-api-contracts.md](../website-builder/02-api-contracts.md), [ADR-011](../../adr/ADR-011-server-side-tools.md).
- **server-side, global** (`time.now`, `GLOBAL_SERVER_SIDE_TOOLS`, [ADR-026](../../adr/ADR-026-global-server-side-tools-and-time-now.md)) — исполняет **backend** немедленно в tool-loop (как `site.*`), но **НЕ требует проекта** и предлагается Claude **всегда** (включая «чистый чат» без проекта). В `toolCalls[]` наружу **НЕ** попадает. Контракт — [§`time.now`](#timenow--server-side-global-tool-adr-026) ниже.
- Orchestrator различает класс по доменному имени (статические реестры `SERVER_SIDE_TOOLS = {site.*}`, `GLOBAL_SERVER_SIDE_TOOLS = {time.now, quiz.generate, image.generate}` ([ADR-057](../../adr/ADR-057-study-learn-quiz-contract.md)/[ADR-058](../../adr/ADR-058-image-generation.md)), непересекающиеся). domain↔anthropic
  mapping (точка→подчёркивание) расширяется server-side именами (`site.write_file ↔ site_write_file`, `time.now ↔ time_now`, …). Guard на число
  server-side раундов — `MAX_SERVER_TOOL_ROUNDS` (дефолт 16) — общий для project-scoped и global server-side раундов.
- **Гейтинг по наличию проекта ([ADR-022](../../adr/ADR-022-optional-project-and-tool-gating.md)):** `site.*` (`SERVER_SIDE_TOOLS`) предлагаются Claude **только** когда у сессии есть `project_id` (создана с `projectId`). В «чистом чате» (`chat_sessions.project_id IS NULL`) `site.*` в tool-набор **не включаются** — Claude их не видит и не вызывает. **`time.now` (`GLOBAL_SERVER_SIDE_TOOLS`) под этот гейт НЕ подпадает** — предлагается всегда ([ADR-026 §3](../../adr/ADR-026-global-server-side-tools-and-time-now.md)). См. [03-architecture.md §Гейтинг tools](03-architecture.md#гейтинг-site-tools-по-наличию-проекта-adr-022).

## `time.now` — server-side global tool ([ADR-026](../../adr/ADR-026-global-server-side-tools-and-time-now.md))
Инструмент текущей даты/времени. Исполняет **backend** в tool-loop (без round-trip к iOS, как `site.*`), но **БЕЗ проекта** — доступен в любом ходе, включая основной flow чат-агрегатора ([ADR-022](../../adr/ADR-022-optional-project-and-tool-gating.md)). Решает репорт «модель отвечает 2024 год»: системный промт статичен и не несёт даты, модель получает время только из результата `time.now`. Не мутирующий (нет `tool_mutation` audit). В `toolCalls[]` наружу не отдаётся (исполнен на бэке).

### Args (`TimeNowArgs`, Pydantic v2, `extra="forbid"`)
```json
{ "tz": "Europe/Moscow" }
```
- `tz` (опц., `str | None`, default `null`) — IANA-имя зоны (напр. `Europe/Moscow`, `America/New_York`). Лимит длины `≤ 64` символа ([Q-026-1](../../99-open-questions.md)). При отсутствии → результат только в UTC.
- `extra="forbid"`: любой иной ключ → ошибка валидации args.

### Result
```json
{
  "utc": "2026-06-10T14:23:05.123456+00:00",
  "unix": 1781446985,
  "weekday": "Wednesday",
  "timezone": "Europe/Moscow",
  "local": "2026-06-10T17:23:05.123456+03:00"
}
```
- `utc` — **всегда**: текущее UTC, ISO8601 (RFC3339) с offset `+00:00`.
- `unix` — **всегда**: целочисленный Unix timestamp (секунды, UTC).
- `weekday` — **всегда**: английское имя дня недели по UTC-дате (`Monday`..`Sunday`).
- `timezone` — **только** при заданном валидном `tz`: нормализованное IANA-имя.
- `local` — **только** при заданном валидном `tz`: ISO8601 с локальным offset.
- Без `tz` → `timezone`/`local` **опущены** (только UTC-набор).

### Ошибки и инварианты
- **Невалидный/неизвестный `tz`** (не парсится `zoneinfo` / `ZoneInfoNotFoundError` / длина > 64) → **tool-result error** `{"error":{"code":"invalid_timezone","message":"..."}}` (через `ToolExecution.error`), **НЕ** падение хода (не `422`, не `502`). Claude получает машиночитаемую ошибку и может повторить без `tz`/с корректной зоной; ход продолжается.
- **UTC-набор от tz-базы не зависит** (вычисляется от `datetime.UTC`) и доступен всегда. Локальное время по `tz` требует tz-базы в образе — обеспечена pure-Python зависимостью `tzdata` ([TD-019](../../100-known-tech-debt.md) **Resolved 2026-06-10**, вариант A); `tz` в prod работает. Невалидная/мусорная зона по-прежнему деградирует к tool-result error `invalid_timezone` (резолв ловит `ZoneInfoNotFoundError`/`ValueError`/`OSError`).
- **Биллинг:** раунд `time.now` не добавляет списаний — 1 кредит = 1 сообщение ([ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md)); списание один раз на финальном `assistant_message`.
- **Clock-провайдер:** время берётся через инъектируемый `Clock` (детерминизм qa, [ADR-026 §8](../../adr/ADR-026-global-server-side-tools-and-time-now.md), [06-testing-strategy.md](../../06-testing-strategy.md)), не прямой `datetime.now()`.

## Tools (backend ↔ iOS, client-side) — строго типизированные схемы
Backend только инициирует tool-call; исполняет клиент. Все мутирующие tools (`files.write`, `files.mkdir`, `calendar.create_events`, `reminders.create`) → audit-запись. Server-side `site.write_file`/`site.delete` также мутирующие (audit) — см. website-builder.

### Имена tools: доменный (iOS) vs Anthropic-формат
Публичный контракт с iOS (ТЗ §5) использует **доменные имена с точкой** (`files.read`, `calendar.create_events`, …). Anthropic Messages API требует имя tool по шаблону `^[a-zA-Z0-9_-]{1,128}$` — **точка недопустима**, dotted-имя → `400 invalid_request_error` (BUG-3, воспроизведено: dotted→400, underscore→200).

**Решение (без breaking change §5):** ввести двунаправленный маппинг `domain-name (точка) ↔ anthropic-name (подчёркивание)`. Преобразование детерминированное — замена `.`→`_`:

| Domain-name (iOS-facing, публичный) | Anthropic-name (только в Anthropic tool definitions) |
|---|---|
| `files.read` | `files_read` |
| `files.write` | `files_write` |
| `files.list` | `files_list` |
| `files.mkdir` | `files_mkdir` |
| `calendar.read` | `calendar_read` |
| `calendar.create_events` | `calendar_create_events` |
| `reminders.read` | `reminders_read` |
| `reminders.create` | `reminders_create` |

**Правила маппинга (нормативно):**
- Маппинг — единственный источник истины для соответствия имён; набор tools фиксирован (8 шт.), поэтому маппинг — статическая таблица (двунаправленный dict), а не «слепое» преобразование строк на лету. Обратный маппинг (`anthropic-name → domain-name`) валидирует, что Claude вернул известный tool; неизвестное имя → ошибка обработки tool_use (трактуется как upstream-аномалия, не доходит до iOS).
- При **сборке запроса** к Anthropic (`messages.create`, поле `tools[].name`) backend подставляет **anthropic-name**.
- При **парсинге ответа** Claude (`content` block `type=tool_use`, поле `name`) backend применяет **обратный маппинг** → доменное имя. Наружу — в `toolCall.name` ответов `/v1/chat/run` и `/v1/chat/tool-result`, а также в `tool_calls.tool_name` (БД/audit) — идёт **только доменный формат с точкой**.
- Строгая типизация args/result привязана к **доменным именам** (таблица схем ниже не меняется). Anthropic-имена — исключительно транспортная деталь слоя Anthropic-клиента и нигде, кроме поля `tools[].name`/`tool_use.name` протокола Anthropic, не фигурируют.
- Публичный tool-контракт с iOS (`toolCall.name`, схемы args/result) **не меняется** — это не breaking change.

| Tool | Тип | Args schema | Result schema |
|---|---|---|---|
| `files.read` | read | `{ "path": string }` | `{ "path": string, "content": string, "encoding": "utf8\|base64", "size": int }` |
| `files.write` | mutate | `{ "path": string, "content": string, "encoding": "utf8\|base64", "overwrite": bool }` | `{ "path": string, "bytesWritten": int }` |
| `files.list` | read | `{ "path": string, "recursive": bool }` | `{ "entries": [ { "name": string, "path": string, "isDir": bool, "size": int } ] }` |
| `files.mkdir` | mutate | `{ "path": string, "createIntermediates": bool }` | `{ "path": string, "created": bool }` |
| `calendar.read` | read | `{ "start": "ISO8601 datetime", "end": "ISO8601 datetime", "calendarId": string? }` ([ADR-027](../../adr/ADR-027-calendar-read-contract-alignment.md)) | `{ "events": [ { "id": string, "title": string, "start": "ISO8601 datetime", "end": "ISO8601 datetime", "location": string?, "notes": string? } ] }` |
| `calendar.create_events` | mutate | `{ "events": [ { "title": string, "start": "ISO8601 datetime", "end": "ISO8601 datetime", "location": string?, "notes": string?, "calendarId": string? } ] }` | `{ "created": [ { "id": string, "title": string } ] }` |
| `reminders.read` | read | `{ "listId": string?, "includeCompleted": bool }` | `{ "reminders": [ { "id": string, "title": string, "due": "ISO8601"?, "completed": bool, "notes": string? } ] }` |
| `reminders.create` | mutate | `{ "reminders": [ { "title": string, "due": "ISO8601"?, "notes": string?, "listId": string? } ] }` | `{ "created": [ { "id": string, "title": string } ] }` |

### Общие правила схем
- Все схемы — Pydantic v2, `extra='forbid'`.
- Даты — ISO8601 (RFC3339), UTC или с offset. **Исключение:** календарные `start`/`end` (`calendar.read`, `calendar.create_events`) — ISO8601-datetime в локальном времени **без** offset (naive local), см. раздел «Контракт календарных инструментов: `start`/`end`» ниже и [ADR-027](../../adr/ADR-027-calendar-read-contract-alignment.md).
- `path` валидируется как относительный/безопасный (без `..`-traversal) на стороне валидатора backend; фактический доступ — ответственность клиента.
- `error` (в tool-result) имеет форму `{ "code": string, "message": string }`; при `error` backend передаёт Claude tool_result с `is_error=true`.

### Контракт календарных инструментов: `start`/`end` (нормативно, [ADR-027](../../adr/ADR-027-calendar-read-contract-alignment.md))
**Единый контракт диапазона для `calendar.read` и `calendar.create_events`** (полная консистентность, [ADR-027](../../adr/ADR-027-calendar-read-contract-alignment.md)):

- **Имена аргументов диапазона — идентичны:** `start` / `end` в обоих инструментах. `calendar.read` использует `start`/`end` (ранее `startDate`/`endDate` — **переименовано**, breaking change); `calendar.create_events` — `events[].start` / `events[].end` (без изменений имён).
- **Формат значения — идентичен:** ISO8601 **datetime** в **локальном времени без timezone-offset**, секундная точность — например `"2026-06-11T09:00:00"`. **Date-only (`"2026-06-11"`) больше не является целевым контрактом** для `calendar.read` (backward-compat date-only не поддерживается, [ADR-027 §Decision 2](../../adr/ADR-027-calendar-read-contract-alignment.md)). Naive local — это сложившийся де-факто формат `create_events`; read выровнен под него. Tz-aware — возможное будущее усиление обоих ([Q-027-1](../../99-open-questions.md)).
- **Семантика диапазона — end-exclusive:** интервал `[start, end)` — `start` включительно, `end` исключительно. «Весь день D» = `start="D T00:00:00"`, `end="D+1 T00:00:00"` (полночь следующего дня), а **не** `end="D T23:59:59"` ([ADR-027 §Decision 5](../../adr/ADR-027-calendar-read-contract-alignment.md)). Это даёт достижимость диапазона по времени внутри дня (например 09:00–18:00) и однозначность смежных дней.
- **Валидация формата — НЕ серверная:** `start`/`end` — простой `str` в Pydantic-схеме (без datetime-валидации), **симметрично для read и create** ([ADR-027 §Decision 3](../../adr/ADR-027-calendar-read-contract-alignment.md)). Формат доводится до модели через `TOOL_DESCRIPTIONS` (см. ниже), фактический парсинг datetime — на стороне iOS (EventKit), как и подобает client-side tool ([ADR-011](../../adr/ADR-011-server-side-tools.md)).
- **Описание для модели (`TOOL_DESCRIPTIONS`) — самодостаточно по формату.** Описания `calendar.read` и `calendar.create_events` обязаны явно указывать ISO8601-datetime-формат `start`/`end` (local, no offset, пример `"2026-06-11T09:00:00"`) и end-exclusive-конвенцию «весь день», чтобы модель генерировала datetime, а не date-only. **Корень устранённого бага:** ранее формат жил только в docs и не доходил до модели — модель генерировала date-only ([ADR-027 §Context](../../adr/ADR-027-calendar-read-contract-alignment.md)).
- **Breaking change iOS-контракта `calendar.read`** ([ADR-027 §Consequences](../../adr/ADR-027-calendar-read-contract-alignment.md)): iOS-клиент обязан читать args `start`/`end` (не `startDate`/`endDate`) и трактовать значения как datetime. Требуется скоординированный релиз iOS. Каталог `/v1/tools` остаётся 14 инструментов — меняется только `inputSchema` записи `calendar.read` (генерируется из `_ARGS_BY_TOOL`). BUG-3 name-map (имена инструментов) не затрагивается — меняются имена **аргументов**, не имя tool.
- **Исторические сессии:** старые `chat_steps`/`tool_calls` хранят прежние `startDate`/`endDate`-вызовы как есть; нормализация истории ([ADR-024](../../adr/ADR-024-history-payload-domain-normalization.md)) не переписывает `tool_use.input`. Миграция не требуется ([Q-027-2](../../99-open-questions.md)).

### blockReason enum (повтор для удобства)
`trial_used | subscription_required | subscription_expired | credits_empty | byok_disabled | byok_invalid | rate_limited | policy_denied | max_tokens | image_credits_empty` (источник — [ADR-004](../../adr/ADR-004-blocked-http-200.md); `max_tokens` добавлен [ADR-025](../../adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md) — обрезка ответа по лимиту output-токенов, в отличие от прочих policy-причин срабатывает **после** начала генерации: `usage`/`messageStepId`/`stepId` присутствуют, кредит не списывается; `image_credits_empty` добавлен [ADR-058](../../adr/ADR-058-image-generation.md) — нехватка кредитов на отдельный дебет генерации изображения, байты не сохранены/rollback; **срабатывает в ЛЮБОМ режиме, включая `byok` и trial** — изображение оплачивается кредитами независимо от `mode`, т.к. генерируется на серверном ключе, [ADR-058 §4](../../adr/ADR-058-image-generation.md)).

---

## GET /v1/tools — каталог инструментов ([ADR-019](../../adr/ADR-019-tools-catalog-endpoint.md))
Машиночитаемый каталог всех поддерживаемых backend tools — **16** (8 client-side `files.*`/`calendar.*`/`reminders.*` + 5 `site.*` + `time.now` [ADR-026](../../adr/ADR-026-global-server-side-tools-and-time-now.md) + `quiz.generate` [ADR-057](../../adr/ADR-057-study-learn-quiz-contract.md) + `image.generate` [ADR-058](../../adr/ADR-058-image-generation.md)). Источник — `src/app/chat/tools.py` (single source of truth: `_ARGS_BY_TOOL`, `MUTATING_TOOLS`, `SERVER_SIDE_TOOLS`, `GLOBAL_SERVER_SIDE_TOOLS`, `anthropic_tool_definitions()`). Эндпоинт **не** параметризуется ни `assistantMode`, ни наличием проекта — возвращает полный технический реестр backend (включая `site.*` и `time.now`). **Видимость инструмента в каталоге ≠ его доступность в текущем ходе:** каталог перечисляет технические возможности, но runtime-предложение модели фильтруется — `site.*` гейтятся наличием `project_id` ([ADR-022](../../adr/ADR-022-optional-project-and-tool-gating.md)), `quiz.generate` — dialog-gate (только `study_learn`, [ADR-057 §4](../../adr/ADR-057-study-learn-quiz-contract.md)), `image.generate` — **key-gate** (предлагается, если задан `OPENAI_API_KEY`; НЕ по `LLM_PROVIDER` — генератор изображений независим от чат-провайдера, [ADR-058 §3](../../adr/ADR-058-image-generation.md)); фильтрация по `assistantMode` — [Q-012-1](../../99-open-questions.md). Server-side инструменты клиент **не вызывает** — они не отдаются в `toolCalls[]` и исполняются на бэкенде, поэтому их видимость в каталоге безопасна. `time.now` предлагается всегда и в каталоге всегда присутствует.

### Auth
- **JWT-protected** (как все `/v1/*`, кроме `/v1/preview/*`): `Authorization: Bearer <JWT>` обязателен. Каталог не секретен, но единообразие gateway-auth и снижение анонимного API-surface — обоснование в [ADR-019](../../adr/ADR-019-tools-catalog-endpoint.md). Клиент к этому моменту уже имеет JWT (получен через `/v1/auth/register`, [ADR-018](../../adr/ADR-018-embedded-auth-issuer.md)).
- Метод `GET` (read-only, кэшируемо). Per-user rate-limit как у прочих read-эндпоинтов.

### Response (200)
```json
{
  "tools": [
    {
      "name": "files.read",
      "description": "Read a file from the user's device.",
      "mutating": false,
      "execution": "client",
      "inputSchema": { "type": "object", "properties": { "path": { "type": "string" } }, "required": ["path"] }
    },
    {
      "name": "site.write_file",
      "description": "Write or overwrite a file in the website project...",
      "mutating": true,
      "execution": "server",
      "inputSchema": { "type": "object", "properties": { "...": {} } }
    }
  ]
}
```
- `name` — **доменное** имя с точкой (как в публичном iOS-контракте), НЕ anthropic-underscore (`files_read` — деталь Anthropic-транспорта, BUG-3).
- `description` — из `descriptions` в `anthropic_tool_definitions()`.
- `mutating` — `name ∈ MUTATING_TOOLS` (требует audit при исполнении).
- `execution` — `"server"` если `name ∈ SERVER_SIDE_TOOLS ∪ GLOBAL_SERVER_SIDE_TOOLS` (`site.*` — [ADR-011](../../adr/ADR-011-server-side-tools.md); `time.now` — [ADR-026](../../adr/ADR-026-global-server-side-tools-and-time-now.md); исполняет backend); иначе `"client"` (исполняет iOS).
- `inputSchema` — JSON Schema args (`_ARGS_BY_TOOL[name].model_json_schema()`).
- Порядок — детерминированный (по `_ARGS_BY_TOOL`).

### Полный список (16)
| name | execution | mutating |
|---|---|---|
| files.read | client | нет |
| files.write | client | **да** |
| files.list | client | нет |
| files.mkdir | client | **да** |
| calendar.read | client | нет |
| calendar.create_events | client | **да** |
| reminders.read | client | нет |
| reminders.create | client | **да** |
| site.write_file | **server** | **да** |
| site.preview | **server** | нет |
| site.list | **server** | нет |
| site.read | **server** | нет |
| site.delete | **server** | **да** |
| time.now | **server** (global, [ADR-026](../../adr/ADR-026-global-server-side-tools-and-time-now.md)) | нет |
| quiz.generate | **server** (global, [ADR-057](../../adr/ADR-057-study-learn-quiz-contract.md)) | нет |
| image.generate | **server** (global, [ADR-058](../../adr/ADR-058-image-generation.md)) | **да** |

> `time.now`/`quiz.generate`/`image.generate` — **global** server-side tools (`execution=server`, проекта не требуют; `GLOBAL_SERVER_SIDE_TOOLS = {time.now, quiz.generate, image.generate}`). В отличие от `time.now` (предлагается всегда), `quiz.generate` предлагается **только** при `dialogMode=study_learn` ([ADR-057](../../adr/ADR-057-study-learn-quiz-contract.md)), а `image.generate` — **только если задан `OPENAI_API_KEY`** (key-gate, независим от `LLM_PROVIDER`; Anthropic-инстанс с ключом тоже генерирует, [ADR-058 §3](../../adr/ADR-058-image-generation.md)) — каталог `/v1/tools` перечисляет полный технический реестр, runtime-предложение фильтруется режимом/наличием ключа. domain↔anthropic: `quiz.generate ↔ quiz_generate`, `image.generate ↔ image_generate`.

**Коды:** `200`; `401` нет/невалидный JWT; `429` rate-limit.

## GET /v1/models — список доступных моделей инстанса ([ADR-034](../../adr/ADR-034-user-model-selection.md))

Источник для селектора модели в композере iOS. Возвращает модели **активного провайдера** этого инстанса из allowlist (`ANTHROPIC_MODELS`/`OPENAI_MODELS`, выбор по `LLM_PROVIDER`).

### Auth
- **JWT-protected** (как `GET /v1/tools`, [ADR-019](../../adr/ADR-019-tools-catalog-endpoint.md)): `Authorization: Bearer <JWT>` обязателен. Список не секретен, контур авторизации единый. Per-user rate-limit как у прочих read-эндпоинтов (`enforce_other_limits`). Метод `GET` (read-only, кэшируемо).

### Response (200)
```json
{
  "models": [
    { "id": "gpt-4o", "displayName": "GPT-4o", "default": true },
    { "id": "gpt-4o-mini", "displayName": "GPT-4o mini", "default": false }
  ]
}
```
- `id` — провайдерный id модели, передаётся обратно в `POST /v1/chat/run` `model`.
- `displayName` — человекочитаемое имя для UI (из allowlist-объекта `id→displayName`).
- `default` (bool) — ровно одна модель `true` (дефолтная модель инстанса = `ANTHROPIC_MODEL`/`OPENAI_MODEL` активного провайдера). Дефолт всегда присутствует в списке (добавляется, если allowlist его не содержит) и идёт **первым**; остальные — в порядке вставки allowlist.
- **Пустой allowlist** (env не задан / невалиден) ⇒ ровно один элемент = дефолтная модель инстанса (`displayName = id`, `default:true`) — обратная совместимость ([ADR-034 §1–2](../../adr/ADR-034-user-model-selection.md)).
- Контракт ответа провайдер-агностичен (один формат); наполнение — модели активного провайдера.

**Коды:** `200`; `401` нет/невалидный JWT; `429` rate-limit.

## GET /v1/presets — пресеты промтов ([ADR-035](../../adr/ADR-035-prompt-presets-endpoint.md))

Источник для чипов-пресетов на главном экране чата iOS (экран 4). Тап по чипу подставляет `prompt` в композер. Набор и тексты меняются деплоем backend **без релиза iOS-приложения**. Провайдер/инстанс-агностично: идентичный ответ на всех 3 инстансах. Источник — статический реестр в коде (`src/app/chat/presets.py`, single source of truth, по образцу [`GET /v1/tools`](#get-v1tools--каталог-инструментов-adr-019)).

### Auth
- **JWT-protected** (как `GET /v1/tools`/`GET /v1/models`): `Authorization: Bearer <JWT>` обязателен. Каталог не секретен, контур авторизации единый. Per-user rate-limit как у прочих read-эндпоинтов (`enforce_other_limits`). Метод `GET` (read-only, без побочных эффектов: не создаёт сессию, не пишет ledger/audit).

### Query-параметры (локализация, [ADR-049](../../adr/ADR-049-presets-localization.md))
- **`locale` (опц., str).** Явный выбор локали каталога. Допустимый набор — поддерживаемые локали (`en`, `ru`; расширяется). Нормализуется `strip().lower()`. **Явно указанное значение вне набора → `422`** (`"locale '<x>' is not supported"`) — симметрично строгому `422 unsupported_model` ([ADR-034 §3](#model-опц-session-fixed-adr-034)); молчаливой подмены явного запроса нет.

### Резолвинг локали (порядок, [ADR-049 §3](../../adr/ADR-049-presets-localization.md))
Первый сработавший шаг выигрывает:
1. **Query `?locale=`** — валидное значение из набора; невалидное → `422` (см. выше).
2. **`Accept-Language`** (если query нет) — первый **поддерживаемый** primary-subtag: значение делится по `,`, отбрасывается `;q=...`, берётся часть до `-` в lower (`ru-RU`→`ru`, `en-US`→`en`); первый subtag из набора — результат. Ни одного поддерживаемого / пусто / нераспознано → **тихий fallback** к шагу 3 (заголовок не строго клиент-контролируем → без `422`).
3. **`PRESETS_DEFAULT_LOCALE`** — per-instance дефолт (env, [07-deployment.md](../../07-deployment.md#конфигурация-env); avelyra=`ru`, остальные=`en`). Значение env вне набора → graceful fallback `en` + WARNING (не startup-crash).
4. **`en`** — финальный fallback (канон).

### Response (200)
```json
{
  "locale": "ru",
  "presets": [
    {
      "id": "plan_week",
      "title": "Планирование недели",
      "icon": "calendar",
      "prompt": "Помоги спланировать предстоящую неделю. Расспроси меня о приоритетах, сроках и обязательствах, а затем предложи сбалансированное расписание по дням."
    }
  ]
}
```
- `locale` ([ADR-049 §5](../../adr/ADR-049-presets-localization.md), **аддитивно**) — фактически отданная локаль (из поддерживаемого набора; результат резолвинга). Старые клиенты игнорируют поле.
- `id` — стабильный slug (`[a-z0-9_]`, snake_case), уникален в наборе; стабилен между релизами. **Не локализуется** (общий для всех локалей).
- `title` — отображаемое имя чипа (на выбранной локали).
- `icon` — имя **SF Symbol** (например `calendar`, `doc.text`, `camera`); клиент рендерит `Image(systemName:)`, при отсутствии символа — клиентский fallback. Не emoji ([ADR-035 §4](../../adr/ADR-035-prompt-presets-endpoint.md)). **Не локализуется** (стабильный ресурс iOS).
- `prompt` — plain-text (на выбранной локали), подставляется в композер при тапе (без шаблонов/плейсхолдеров на старте).
- Порядок элементов = порядок чипов на экране (детерминированный, порядок объявления в реестре) — **един во всех локалях**. Все 4 поля пресета обязательны и непусты.
- **Per-field EN-fallback:** если у выбранной локали не заполнено какое-то поле пресета, оно берётся из EN (канон); незаполненная/неизвестная локаль целиком → EN-каталог.

**Дефолтный набор (7, со скрина):** `plan_week`, `meeting_notes`, `tasks_from_photo`, `design_brief`, `daily_review`, `summarize_text`, `project_structure` — EN-тексты в [ADR-035 §3](../../adr/ADR-035-prompt-presets-endpoint.md), RU-тексты в [ADR-049 §1.1](../../adr/ADR-049-presets-localization.md).

**Совместимость:** без env и без запроса локали (`locale` отсутствует, `Accept-Language` без поддерживаемых, дефолт `en`) → EN-ответ как раньше; поле `locale` при этом = `"en"`. Без миграции; провайдер-агностично ([ADR-033](../../adr/ADR-033-llm-provider-abstraction.md)).

**Коды:** `200`; `401` нет/невалидный JWT; `422` явный `?locale=` вне набора; `429` rate-limit.
