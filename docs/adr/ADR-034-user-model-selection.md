# ADR-034 — Выбор модели пользователем (allowlist per-provider, `GET /v1/models`, session-fixed `model`)

- **Статус:** Accepted
- **Дата:** 2026-06-17
- **Связано:** [ADR-033](ADR-033-llm-provider-abstraction.md) (провайдер-абстракция LLM — фундамент), [ADR-001](ADR-001-stack-choice.md) (стек/монолит), [ADR-006](ADR-006-credit-billing-and-subscription-grant.md) (1 кредит = 1 сообщение), [ADR-012](ADR-012-assistant-mode-vs-billing-mode.md) (session-fixed атрибуты), [ADR-016](ADR-016-extended-byok-statuses.md) (активная модель в BYOK-ответе), [ADR-019](ADR-019-tools-catalog-endpoint.md) (паттерн JWT-protected каталога), [ADR-022](ADR-022-optional-project-and-tool-gating.md) (session-fixed `projectId`, паттерн «resume → из сессии, поле игнорируется»), [ADR-017](ADR-017-shared-server-traefik-deploy.md) (мульти-инстанс / per-instance env)
- **Ревизия 2026-07-09 ([INDEX §Ревизии](INDEX.md#ревизии)):** §3 (error-код model-гейта) пересмотрен — вместо generic `validation_error` вводится **выделенный машинный код `unsupported_model`** (subclass `UnsupportedModelError`), для симметрии с `unsupported_dialog_mode` ([ADR-055](ADR-055-dialog-mode.md)). Тело §3 ниже сохранено (immutability); актуальный контракт — в callout «Ревизия 2026-07-09» внутри §3. Устраняет docs↔code mismatch (в коде исходно был generic `validation_error`, а проза перечисляла `unsupported_model` как именованный код).

## Контекст

В композере чата iOS есть селектор модели (на скриншоте «GPT-5.5»). Сейчас модель **жёстко фиксирована per-instance env**: `ANTHROPIC_MODEL=claude-sonnet-4-5` (broadnova/avelyra), `OPENAI_MODEL=gpt-4o` (orvianix). Поля `model` в `/v1/chat/run` нет; эндпоинта списка моделей нет. Внутри клиента (`anthropic_client.py`/`openai_client.py`) модель берётся из `settings.<provider>_model` ([ADR-033 §9](ADR-033-llm-provider-abstraction.md)).

Цель (Поставка 1 плана model/presets): дать пользователю выбрать модель из **разрешённого инстансом набора** и зафиксировать выбор за чат-сессией.

Ограничения и инварианты (НЕ пересматривать):
- **Каждый инстанс — ОДИН провайдер** ([ADR-033](ADR-033-llm-provider-abstraction.md)). Allowlist моделей инстанса — модели **активного** провайдера. Кросс-провайдерный выбор невозможен по построению (нельзя выбрать Claude на OpenAI-инстансе).
- **Обратная совместимость — КРИТИЧНО.** Без `model` в запросе и без allowlist в env поведение **идентично текущему** (дефолтная модель инстанса = `<provider>_model`). Существующие инстансы/тесты не меняют поведение.
- **Биллинг неизменен** ([ADR-006](ADR-006-credit-billing-and-subscription-grant.md)): 1 кредит = 1 сообщение независимо от модели. Тарификация по модели — отложено ([Q-034-1](../99-open-questions.md)).
- **Один провайдер на инстанс** ⇒ модель фиксируется за сессией (как `mode`/`assistantMode`/`projectId`, [ADR-012](ADR-012-assistant-mode-vs-billing-mode.md)/[ADR-022](ADR-022-optional-project-and-tool-gating.md)), не за каждым сообщением.

## Решение

### 1. Allowlist моделей per-provider (config)

Новые env (`config.py`), парсятся по образцу `token_products()`/`adapty_product_tokens()`:

- `ANTHROPIC_MODELS` (alias env, raw JSON-строка; поле `anthropic_models_raw`, дефолт `"{}"`).
- `OPENAI_MODELS` (alias env, raw JSON-строка; поле `openai_models_raw`, дефолт `"{}"`).

**Формат:** JSON-объект `{ "<model-id>": "<displayName>" }`. `<model-id>` — провайдерный id, передаваемый в API (например `gpt-4o`, `claude-sonnet-4-5`); `<displayName>` — человекочитаемое имя для UI (например `"GPT-4o"`, `"Claude Sonnet 4.5"`).

**Метод `Settings.allowed_models() -> dict[str, str]`** (единый, провайдер-aware):
1. Выбирает raw по активному `llm_provider` (`openai` → `openai_models_raw`; иначе → `anthropic_models_raw`).
2. Парсит JSON. Невалидный JSON / не-объект → `{}` (как `token_products()`). Сохраняются только пары `str → непустой str` (ключ — непустая строка после `strip`, значение — непустая строка; прочее отбрасывается).
3. **Фолбэк обратной совместимости:** если результат пуст — возвращается `{ <active_default_model>: <active_default_model> }`, где `<active_default_model>` = `openai_model` при `openai`, иначе `anthropic_model`. То есть пустой allowlist ⇒ единственная дефолтная модель инстанса (displayName = id), что воспроизводит текущее поведение.

**Метод `Settings.default_model() -> str`** — активная дефолтная модель инстанса: `openai_model` при `provider=openai`, иначе `anthropic_model`. **Инвариант:** `default_model()` ВСЕГДА присутствует в `allowed_models()` (если задан непустой allowlist, не содержащий дефолта, дефолт **добавляется** к набору — см. §2; гарантирует, что дефолт всегда выбираем и помечается `default:true`).

> Парсинг — pure (без I/O), кэшируется через `get_settings()` lru_cache на время процесса (как `token_products()`).

### 2. `GET /v1/models`

JWT-protected (`CurrentUser`), как [`GET /v1/tools`](ADR-019-tools-catalog-endpoint.md) — единый авторизационный контур `/v1/*`; список не секретен, но контур общий. Per-user rate-limit как прочие reads (`enforce_other_limits`).

**Response 200:**
```json
{ "models": [ { "id": "gpt-4o", "displayName": "GPT-4o", "default": true } ] }
```
- `models[]` — модели **активного провайдера инстанса** из `allowed_models()`.
- `default: bool` — ровно одна модель помечена `true` (= `default_model()`). Если allowlist задан и НЕ содержит дефолта — дефолт **добавляется** к набору и помечается `default:true` (displayName = id), остальные `false`.
- Порядок: дефолт первым, далее в порядке вставки allowlist-объекта (insertion order JSON-парсера сохраняется), без дубля дефолта.
- Пустой allowlist ⇒ ровно один элемент = дефолтная модель инстанса (`default:true`) — обратная совместимость.

**Коды:** `200`; `401`; `429`. Эндпоинт **не** зависит от провайдера в контракте: один и тот же ответ-формат, наполнение — модели активного провайдера.

### 3. `ChatRunRequest.model: str | None` — session-fixed

- Новое **опциональное** поле `model` (`str | None`, дефолт `None`) в `ChatRunRequest` (`schemas/chat.py`). Если строка — после `strip` непустая (пустая/whitespace при наличии поля → `422`, симметрично `projectId`).
- **Session-fixed** (как `mode`/`assistantMode`/`projectId`, [ADR-012](ADR-012-assistant-mode-vs-billing-mode.md)/[ADR-022](ADR-022-optional-project-and-tool-gating.md)): фиксируется на сессию при **создании**; при **resume** (`sessionId` задан и сессия существует) поле запроса **игнорируется** (не ошибка) — модель берётся из `chat_sessions.model`.
- **Хранение:** новая колонка `chat_sessions.model` (`Text`, **nullable**; миграция 0010). `NULL` = «дефолтная модель инстанса» (обратная совместимость: существующие строки и запросы без `model` → `NULL`).
- **Резолюция при создании сессии:** `resolved_model = model.strip() if model is not None else None`. То есть отсутствие поля → `NULL` (а не подстановка дефолта в БД) — чтобы сессии, созданные до фичи / без выбора, оставались «дефолт инстанса» даже при будущей смене дефолта env.

**Валидация модели (при создании сессии, до записи):** если `model` задан и `model.strip() NOT IN allowed_models()` → **`ValidationFailedError` (HTTP 422, `code="validation_error"`)** с понятным сообщением `"model '<x>' is not available on this instance"` (детализатор `unsupported_model` в тексте сообщения — стилистически симметрично `unsupported_media_type` PDF-reject [ADR-033](ADR-033-llm-provider-abstraction.md); отдельного error-`code` не вводится). Тихий фолбэк на дефолт **отклонён** — явный контракт лучше: клиент строит селектор из `GET /v1/models`, значит присылает только валидные id; неизвестный id — ошибка клиента, а не повод молча сменить модель. Валидация выполняется **только при создании сессии** (на resume поле игнорируется, повторная валидация не нужна — сохранённая модель уже валидна на момент записи).

> **Ревизия 2026-07-09 (актуальный контракт, супершедит абзац выше в части error-кода).** Решение «отдельного error-`code` не вводится» **пересмотрено**: model-гейт бросает **выделенный `UnsupportedModelError(ValidationFailedError)` с `code="unsupported_model"`**, HTTP 422, сообщение прежнее (`"model '<x>' is not available on this instance"`). **Причина пересмотра:** на момент ADR-034 (2026-06-17) паттерна именованных кодов для instance-capability-гейтов ещё не было (симметрия бралась с `unsupported_media_type`, который сам эмитит generic `validation_error`). [ADR-055](ADR-055-dialog-mode.md) (2026-07-09) ввёл `UnsupportedDialogModeError(code="unsupported_dialog_mode")` для **структурно идентичного** гейта (session-fixed атрибут, валидируется на создании сессии против instance-allowlist, 422 если вне набора) — потому что клиент строит меню из возможностей инстанса и различает «не поддерживается на инстансе» vs «тело невалидно» по машинному коду, а не по тексту. Тот же довод дословно применим к `model` (селектор строится из `GET /v1/models`). Асимметрия «model → generic, dialogMode → named» — случайна (ADR-034 предшествовал паттерну), а не принципиальна; унифицируется **вверх** (оба именованные). Изменение **аддитивно** (HTTP-статус 422 не меняется, меняется только `error.code` с `validation_error` на `unsupported_model`), сделано в момент, когда iOS ещё не завязан на поведение model-гейта. **Обработчик `AppError` (`main.py`) сериализует `exc.code`** → в wire `{"error":{"code":"unsupported_model", ...}}`. Реализация — задача backend (см. [chat-orchestrator/02-api-contracts.md §model](../modules/chat-orchestrator/02-api-contracts.md#model-опц-session-fixed-adr-034)); ADR-034 код не пишет.

### 4. Orchestrator → клиент: прокидывание модели

- **Сигнатура `LLMClient.create_message` расширяется аддитивным kwarg** (`llm_client.py`, Protocol + обе реализации):
  ```
  async def create_message(
      self, *, system_prompt: str,
      messages: list[NeutralMessage],
      tools: list[dict],
      attachments: PreparedAttachments | None = None,
      api_key: str | None = None,
      model: str | None = None,        # NEW — ADR-034
  ) -> LLMResult: ...
  ```
  - `model=None` (дефолт) ⇒ клиент берёт **свою** дефолтную модель (`settings.<provider>_model`) — текущее поведение, ничего не меняется для существующих вызовов/тестов.
  - `model="<id>"` ⇒ клиент использует переданный id в провайдерном вызове вместо дефолта. Реверс-резолюция и провайдер-специфика — внутри клиента (как и вся wire-специфика, [ADR-033 §3](ADR-033-llm-provider-abstraction.md)).
- **Orchestrator** (`orchestrator.py`):
  - При создании сессии передаёт `resolved_model` в `repository.get_or_create_session(..., model=resolved_model)` (новый kwarg, дефолт `None`, как `assistant_mode`/`title`).
  - При генерации передаёт в `create_message(..., model=sess.model or None)`. `sess.model` (`NULL`) → `None` → клиент берёт дефолт. То есть orchestrator **никогда не подставляет дефолтную модель сам** — это остаётся ответственностью клиента (единая точка дефолта, провайдер-агностично).
- **`repository.get_or_create_session`** (`repository.py`): новый kwarg `model: str | None = None`; пишется в `chat_sessions.model` **только при создании** новой сессии (как `assistant_mode`/`project_id`/`title`); на resume не трогается.

### 5. Возвращаемая `usage.model`

`LLMResult.usage.model` ([ADR-033 §1](ADR-033-llm-provider-abstraction.md)) уже отражает фактически использованную модель (из ответа провайдера). С выбором модели это естественно показывает выбранную модель — отдельных правок не требуется.

### 6. Биллинг

Без изменений ([ADR-006](ADR-006-credit-billing-and-subscription-grant.md)): 1 кредит = 1 сообщение независимо от выбранной модели. Дифференцированная тарификация по модели (премиум-модели дороже) — **отложено** ([Q-034-1](../99-open-questions.md), [TD-025](../100-known-tech-debt.md)).

### 7. Provider-нюанс

- На anthropic-инстансах `ANTHROPIC_MODELS` (Claude-id) активен; на orvianix `OPENAI_MODELS` (OpenAI-id). `allowed_models()` выбирает набор по `LLM_PROVIDER`; `GET /v1/models` отражает активный провайдер.
- Allowlist гарантирует корректность model↔provider: id из allowlist всегда принадлежит активному провайдеру (оператор кладёт в env корректные id). Чужой id невозможно выбрать — его нет в `allowed_models()`, валидация §3 даёт `422`.
- **BYOK ([ADR-016](ADR-016-extended-byok-statuses.md)):** в этой поставке BYOK-активная-модель (`activeModel` в BYOK-ответах) **не меняется** — остаётся `BYOK_DEFAULT_MODEL`/`OPENAI_BYOK_DEFAULT_MODEL`. Однако session-fixed `model` применяется и к `mode=byok`-сессиям (передаётся в `create_message`, который при BYOK использует ключ пользователя). Совмещение выбора модели с BYOK-`activeModel`-отчётом — вне scope этой поставки (текущий контракт `activeModel` сохраняется без регрессии).

## Альтернативы

- **Per-message `model` (не session-fixed).** Отклонено: ломает консистентность сессии (история одного диалога на разных моделях), расходится с паттерном session-fixed атрибутов ([ADR-012](ADR-012-assistant-mode-vs-billing-mode.md)/[ADR-022](ADR-022-optional-project-and-tool-gating.md)); инстанс одно-провайдерный, смена модели внутри диалога — отдельная фича при необходимости.
- **Тихий фолбэк на дефолт при неизвестном `model`.** Отклонено: маскирует ошибку клиента, делает контракт неявным; `422` — честнее и проще для отладки.
- **Allowlist как массив id (без displayName).** Отклонено: UI нужен человекочитаемый ярлык; объект `id→displayName` совмещает allowlist и метаданные UI в одном env без отдельного источника.
- **Отдельная таблица моделей в БД.** Отклонено: избыточно для статичного per-instance набора; env проще, согласуется с per-instance конфигурацией ([ADR-017](ADR-017-shared-server-traefik-deploy.md)) и с существующими env-каталогами (`TOKEN_PRODUCTS`).
- **Открытый ввод любой model-строки (без allowlist).** Отклонено: риск выбора несуществующей/чужой/дорогой модели, нет контроля стоимости/доступности; allowlist даёт оператору контроль.

## Последствия

- **Положительные:** пользователь выбирает модель из контролируемого оператором набора; `GET /v1/models` даёт UI источник для селектора; выбор фиксируется за сессией (консистентная история); обратная совместимость полная (без env/без поля → текущее поведение); провайдер-агностично (один контракт на оба провайдера).
- **Цена:** новая колонка `chat_sessions.model` + миграция 0010; аддитивный kwarg `model` в `create_message` (Protocol + 2 реализации); новый роутер `/v1/models` + схема; провайдер-aware парсинг allowlist в config.
- **Tech debt:** дифференцированная тарификация по модели не реализована ([TD-025](../100-known-tech-debt.md), [Q-034-1](../99-open-questions.md)); presets (предустановленные конфиги модель+режим) — будущие поставки плана model/presets ([Q-034-2](../99-open-questions.md)).
- **Безопасность:** model-id не секрет; валидация по allowlist (не открытый ввод) исключает выбор произвольной upstream-модели; биллинг-инвариант (1 кредит) защищён от обхода выбором модели; model-id под общими size-лимитами тела (не отдельный лимит — короткая строка).
