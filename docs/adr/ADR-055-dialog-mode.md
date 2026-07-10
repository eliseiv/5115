# ADR-055 — Режим диалога (`dialog_mode`): session-fixed, provider-gated

- **Статус:** Accepted
- **Дата:** 2026-07-09
- **Связано:** [ADR-034](ADR-034-user-model-selection.md) (session-fixed выбор модели — прямой прецедент механики), [ADR-012](ADR-012-assistant-mode-vs-billing-mode.md) (разведение `assistant_mode` vs `billing_mode`, паттерн session-fixed атрибутов), [ADR-033](ADR-033-llm-provider-abstraction.md) (один провайдер на инстанс, нормализованный `stop_reason`), [ADR-059](ADR-059-openai-default-provider.md) (Responses API как фундамент Search/reasoning), [ADR-057](ADR-057-study-learn-quiz-contract.md) (квиз предлагается только в `study_learn`), [ADR-004](ADR-004-blocked-http-200.md) (граница business-blocked vs технический 4xx), [ADR-022](ADR-022-optional-project-and-tool-gating.md) (паттерн «resume → из сессии, поле игнорируется»)

## Контекст

На скриншоте меню Modes четыре режима диалога: **Smart / Deep Thinking / Study & Learn / Search**. Сейчас у беседы есть только `assistant_mode` (`chat|code`, [ADR-012](ADR-012-assistant-mode-vs-billing-mode.md)) — «тип ассистента», и `mode` (`credits|byok`, billing) — «способ оплаты». Ни одно из этих полей не выражает «как модель думает над ответом»: Smart — обычный ход; Deep Thinking — reasoning с бюджетом рассуждения; Study & Learn — обучающий стиль с квизами ([ADR-057](ADR-057-study-learn-quiz-contract.md)); Search — веб-поиск в ходе генерации.

Требуется третья, ортогональная ось выбора, зафиксированная так же, как `model`/`assistantMode` ([ADR-034](ADR-034-user-model-selection.md)/[ADR-012](ADR-012-assistant-mode-vs-billing-mode.md)), с обратной совместимостью (существующие сессии/запросы без поля → `smart`).

## Решение

### 1. Новое поле `dialog_mode`, а НЕ расширение `assistant_mode`

`assistant_mode` (`chat|code`) отвечает на вопрос **«какой ассистент»** — продуктовый профиль ответа и состав tool-реестра ([Q-012-1](../99-open-questions.md)): code-режим даёт `files.*`/`site.*`, chat — нет. `dialog_mode` отвечает на **ортогональный** вопрос **«как модель обрабатывает запрос»** — reasoning-бюджет, веб-поиск, обучающий контракт. Эти оси независимы: `code`+`deep_thinking` («глубоко продумать код») и `chat`+`search` («поискать факт в чате») — обе осмысленны. Складывать их в один enum значило бы декартово произведение (`chat_smart`, `code_search`, …) — комбинаторный взрыв, ломающий существующую семантику `assistant_mode` и её гейтинг tools. Поэтому вводится **третье** поле, симметричное `model`/`assistantMode` по механике фиксации, но независимое по смыслу. Итог: у сессии три ортогональных измерения — `mode` (оплата), `assistant_mode` (тип ассистента), `dialog_mode` (режим обработки).

Четыре значения enum `dialog_mode`: `smart`, `deep_thinking`, `study_learn`, `search`.

### 2. Session-fixed (как `model`/`assistantMode`/`projectId`)

`dialog_mode` фиксируется на сессию **при создании** и не меняется на resume — единообразно с `model` ([ADR-034 §3](ADR-034-user-model-selection.md)) и обоснованием оттуда: режим определяет способ генерации всех ходов диалога, смена режима внутри одного диалога дала бы неконсистентную историю (часть ходов reasoning, часть — обычные; часть с веб-поиском, часть без). Смена режима = новая сессия (текущий путь смены `model`/`assistantMode`).

- **Хранение:** колонка `chat_sessions.dialog_mode` (enum `dialog_mode`, `NOT NULL DEFAULT 'smart'`). `server_default` покрывает существующие строки без backfill (как `assistant_mode` в миграции `0004`).
- **Резолюция при создании сессии** (копия механики `resolved_assistant_mode`): значение из запроса → дефолт пользователя (`user_preferences.default_dialog_mode`) → `smart`.
- **Resume:** поле запроса игнорируется (не ошибка), берётся `chat_sessions.dialog_mode` ([ADR-022](ADR-022-optional-project-and-tool-gating.md), паттерн).

### 3. Дефолт пользователя: `user_preferences.default_dialog_mode`

Новая колонка `user_preferences.default_dialog_mode` (enum `dialog_mode`, `NOT NULL DEFAULT 'smart'`) — как `default_assistant_mode` ([ADR-012](ADR-012-assistant-mode-vs-billing-mode.md)). Отдаётся/принимается в `GET`/`PATCH /v1/preferences` (`defaultDialogMode`). Позволяет пользователю задать предпочитаемый режим один раз; запрос без `dialogMode` берёт этот дефолт, затем `smart`.

### 4. Provider-gated: `deep_thinking|study_learn|search` требуют OpenAI

Три «продвинутых» режима опираются на OpenAI Responses API ([ADR-059](ADR-059-openai-default-provider.md)): Deep Thinking — на `reasoning={"effort": ...}` и реплей `reasoning.encrypted_content`; Search — на встроенный tool `{"type": "web_search"}`; Study & Learn — на строгий function-tool `quiz.generate` ([ADR-057](ADR-057-study-learn-quiz-contract.md)). Anthropic-путь ([ADR-033](ADR-033-llm-provider-abstraction.md)) этих механизмов в текущей реализации не несёт, поэтому на anthropic-инстансе `deep_thinking|study_learn|search` **недоступны**. `smart` работает на обоих провайдерах.

Гейт применяется **при создании сессии**: если `dialog_mode ∈ {deep_thinking, study_learn, search}` и активный сервисный провайдер инстанса — не OpenAI, запрос отклоняется **`422 unsupported_dialog_mode`** (`UnsupportedDialogModeError`, по образцу `unsupported_model` [ADR-034 §3](ADR-034-user-model-selection.md)).

**Почему `422`, а не `blocked` ([ADR-004](ADR-004-blocked-http-200.md)).** `blocked`+HTTP 200 — для **бизнес**-ограничений, предсказуемых policy до генерации (нет подписки, кончились кредиты). Запрос несуществующего на инстансе режима — **неверный запрос клиента** (клиент строит меню из возможностей инстанса), а не бизнес-блокировка пользователя. `422` честнее и проще для отладки, как и `unsupported_model`. Поэтому `unsupported_dialog_mode` **не** входит в `blockReason`-enum и не появляется в `policy/effective.reasons[]`.

Гейт по провайдеру завязан на сервисный провайдер инстанса (`LLM_PROVIDER`), а не на BYOK: даже если пользователь принёс OpenAI-ключ на anthropic-инстанс ([ADR-044](ADR-044-multi-provider-byok.md)), продвинутые режимы завязаны на код-путь Responses API, который выбирается по сервисному провайдеру инстанса. Это осознанное упрощение (паритет BYOK-провайдеров с режимами — [TD-030](../100-known-tech-debt.md)).

### 5. Влияние каждого режима на генерацию

- **`smart`** — базовый ход, поведение не изменилось. Провайдер-агностичен (работает и на Anthropic). Дефолт.
- **`deep_thinking`** — принудительно берётся reasoning-модель инстанса `DEEP_THINKING_MODEL`, переопределяя выбор пользователя `sess.model` (включая stale-model guard [ADR-044](ADR-044-multi-provider-byok.md)); в запрос добавляется `reasoning={"effort": DEEP_THINKING_EFFORT}`. Reasoning-items реплеятся в tool-loop (`store=False` + `include=["reasoning.encrypted_content"]`) — иначе OpenAI отвергнет continuation. Обоснование форсирования модели и его следствие для BYOK — [ADR-059 §4](ADR-059-openai-default-provider.md).
- **`study_learn`** — модели дополнительно предлагается function-tool `quiz.generate` ([ADR-057](ADR-057-study-learn-quiz-contract.md)); системный промт получает обучающий суффикс. Квиз возвращается в отдельном поле ответа.
- **`search`** — к нашим function-tools добавляется встроенный `{"type": "web_search"}` (GA-имя, [ADR-059 §2](ADR-059-openai-default-provider.md)); `search_context_size` = `OPENAI_SEARCH_CONTEXT_SIZE`. Цитаты приходят в `message.content[].output_text.annotations` (тип `url_citation`), сохраняются в `content_blocks` и отдаются клиенту через `serverTools`/историю. `web_search_call` в `output[]` **не** порождает клиентский `tool_use` ([ADR-059 §1](ADR-059-openai-default-provider.md)).

Режимный суффикс системного промта компонуется **после** base assistant_mode-промта и workspace-инструкций ([ADR-036](ADR-036-workspaces-implementation.md)) — не ломает prompt-структуру и порядок инъекций.

### 6. Схема запроса и биллинг

- `ChatRunRequest.dialogMode: str | None` — **не `Literal`** (иначе Pydantic даёт generic `422` без машинного кода). Членство в множестве валидируется в оркестраторе при создании сессии → `422 unsupported_dialog_mode` (как `model`, [ADR-034 §3](ADR-034-user-model-selection.md)).
- Биллинг неизменен ([ADR-006](ADR-006-credit-billing-and-subscription-grant.md)): 1 кредит = 1 сообщение независимо от режима. Deep Thinking/Search тратят больше upstream-токенов, но тариф в кредитах не дифференцируется (как и по модели, [TD-025](../100-known-tech-debt.md)). Отдельный дебет есть только у генерации изображений ([ADR-058](ADR-058-image-generation.md)) — это не режим диалога.

## Альтернативы

- **Расширить `assistant_mode` до 6 значений (`chat`/`code`/`smart`/…).** Отклонено: смешивает ортогональные оси «тип ассистента» и «режим обработки», ломает существующий гейтинг tools по `assistant_mode` ([Q-012-1](../99-open-questions.md)) и семантику ADR-012.
- **Per-message `dialogMode` (не session-fixed).** Отклонено: неконсистентная история диалога (reasoning-ходы вперемешку с обычными), расходится с session-fixed паттерном `model`/`assistantMode`/`projectId` ([ADR-034](ADR-034-user-model-selection.md)/[ADR-012](ADR-012-assistant-mode-vs-billing-mode.md)/[ADR-022](ADR-022-optional-project-and-tool-gating.md)).
- **`blocked`+`blockReason=unsupported_dialog_mode` вместо `422`.** Отклонено: `blocked` — для бизнес-ограничений ([ADR-004](ADR-004-blocked-http-200.md)); неверный режим — ошибка запроса, не бизнес-блок. `422` симметрично `unsupported_model`.
- **Тихий фолбэк недоступного режима на `smart`.** Отклонено: маскирует несоответствие клиента возможностям инстанса; явный `422` честнее (тот же аргумент, что для `unsupported_model` [ADR-034](ADR-034-user-model-selection.md)).
- **Реализовать все режимы и на Anthropic сразу.** Отклонено на эту поставку: Anthropic-путь не несёт Responses-механизмов (reasoning-replay, web_search-tool, strict-quiz одним ходом); паритет отложен ([TD-030](../100-known-tech-debt.md)), чтобы не блокировать фичу переписыванием обоих клиентов.

## Последствия

- **Положительные:** четыре режима из меню Modes работают как session-fixed атрибут по устоявшейся механике ([ADR-034](ADR-034-user-model-selection.md)); дефолт пользователя через preferences; обратная совместимость полная (нет поля → `smart`); ортогональность `mode`/`assistant_mode`/`dialog_mode` сохранена и явно зафиксирована.
- **Цена:** новый enum `dialog_mode` + две колонки (`chat_sessions.dialog_mode`, `user_preferences.default_dialog_mode`) + миграция; provider-gate и новый `UnsupportedDialogModeError`; ветвление в оркестраторе (`deep_thinking`/`search`/`study_learn`).
- **Tech debt:** продвинутые режимы работают только на OpenAI-инстансах — паритет с Anthropic отложен ([TD-030](../100-known-tech-debt.md)).
- **Безопасность:** `dialog_mode` не секрет; provider-gate исключает попытку активировать неподдерживаемый режим; биллинг-инвариант (1 кредит) не обходится сменой режима.
