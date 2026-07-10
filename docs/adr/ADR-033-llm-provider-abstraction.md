# ADR-033 — Провайдер-абстракция LLM (Anthropic | OpenAI), один провайдер на инстанс

- **Статус:** Accepted (ревизия 2026-06-19 — PDF-ограничение OpenAI снято)
- **Дата:** 2026-06-16
- **Связано:** [ADR-001](ADR-001-stack-choice.md) (стек/монолит), [ADR-008](ADR-008-provider-tool-use-id.md) (raw provider tool_use.id), [ADR-011](ADR-011-server-side-tools.md), [ADR-016](ADR-016-extended-byok-statuses.md) (BYOK статусы), [ADR-017](ADR-017-shared-server-traefik-deploy.md) (мульти-инстанс), [ADR-020](ADR-020-inline-base64-attachments-mvp.md) (inline base64-вложения), [ADR-021](ADR-021-deterministic-step-order-and-block-normalization.md) (нормализация payload), [ADR-024](ADR-024-history-payload-domain-normalization.md), [ADR-025](ADR-025-parallel-tool-calls-and-max-tokens-truncation.md) (stop_reason-диспетчеризация), [ADR-031](ADR-031-absolute-preview-url.md), [ADR-041](ADR-041-openai-native-pdf-attachment.md) (снимает PDF→422 в §5)

> **Ревизия 2026-06-25 ([ADR-044](ADR-044-multi-provider-byok.md), BYOK-провайдер).** §7 ниже фиксирует «BYOK на инстансе валидирует ключ **своего** провайдера». Для **BYOK** это правило **снято**: провайдер BYOK-ключа определяется **по самому ключу** (детектор префиксов), валидация и генерация byok идут через клиент провайдера ключа **независимо** от `LLM_PROVIDER` — см. [ADR-044](ADR-044-multi-provider-byok.md). Инвариант «один **сервисный** (credits) провайдер на инстанс» (§«Каждый инстанс — ОДИН провайдер», §8) **в силе** — он касается сервисного credits-пути, не BYOK. Фабрика `get_llm_client()` (§8) сигнатуру не меняет; добавлена `llm_client_for(provider)` (ADR-044 §2). Тело ADR-033 не переписывается (immutability).
>
> **Ревизия 2026-06-19 ([ADR-041](ADR-041-openai-native-pdf-attachment.md), [TD-023](../100-known-tech-debt.md) Resolved).** Зафиксированное ниже ограничение «PDF при `provider=openai` → 422 `unsupported_media_type`» (строки в §Решение/§5 и в §Контекст) **снято**: OpenAI теперь принимает PDF (нативная content-часть `file` либо `pypdf`-фолбэк). Тело решения ADR-033 не переписывается (immutability) — актуальный провайдер-маппинг PDF см. [ADR-041](ADR-041-openai-native-pdf-attachment.md) и [chat-orchestrator/03-architecture.md](../modules/chat-orchestrator/03-architecture.md). Остальные инварианты ADR-033 (один провайдер на инстанс, non-streaming, дефолт anthropic) в силе.

## Контекст

Backend жёстко завязан на Anthropic: `AnthropicClient` (`src/app/chat/anthropic_client.py`) — единственный LLM-клиент, инжектится в orchestrator и BYOK; стек содержит только `anthropic` SDK. Цель — развернуть **OpenAI-клон** того же сервиса как 3-й мульти-инстанс ([ADR-017](ADR-017-shared-server-traefik-deploy.md)) под отдельным доменом, **без форка кода**: один и тот же код, провайдер выбирается env `LLM_PROVIDER`.

Ограничения и согласованные решения (НЕ пересматривать):
- Абстракция в **этом же коде** (не форк). `LLM_PROVIDER ∈ {anthropic, openai}`, **дефолт `anthropic`** → живые инстансы `claude-ios`/`avelyra` работают без изменения поведения.
- **Каждый инстанс — ОДИН провайдер.** Его БД хранит wire-формат своего провайдера; кросс-провайдерный реплей в одной БД НЕ требуется и НЕ поддерживается.
- OpenAI через **Chat Completions API** (function-calling + vision), **non-streaming** (паритет с текущим Anthropic-путём, [ADR-025](ADR-025-parallel-tool-calls-and-max-tokens-truncation.md) §non-streaming).
- Модель OpenAI по умолчанию `gpt-4o` (env `OPENAI_MODEL`); BYOK-дефолт-модель OpenAI — отдельный ключ.
- Attachments на OpenAI: картинки (`image_url` data-URI) + текст. **PDF при `provider=openai` → `ValidationFailedError` (422 `unsupported_media_type`)** — Chat Completions vision не принимает PDF-документ.
- Caching: `cache_control` — только Anthropic; у OpenAI авто-кэш промпт-префикса, спец-логики в коде нет.

## Решение

### 1. Нейтральный интерфейс `LLMClient`

Вводится Protocol/ABC `LLMClient` (`src/app/chat/llm_client.py`) — провайдер-агностичный контракт. `AnthropicClient` остаётся как есть (становится реализацией интерфейса), добавляется `OpenAIClient`. Контракт:

```
class LLMClient(Protocol):
    async def create_message(
        self, *, system_prompt: str,
        messages: list[NeutralMessage],      # см. §2 (нейтральная история)
        tools: list[dict],                   # нейтральные определения tools (см. §3)
        attachments: PreparedAttachments | None,  # нейтральные вложения первого turn (см. §4)
        api_key: str | None = None,          # BYOK override
    ) -> LLMResult: ...
    async def validate_key(self, api_key: str) -> KeyValidation: ...
```

- `LLMResult` (нейтральный, замена `AnthropicResult`): `stop_reason: NeutralStopReason`, `content_blocks: list[dict]` (wire-формат **активного провайдера** — для персиста), `usage: LLMUsage`, `text: str`, `tool_uses: list[{id, name(domain), input}]`.
- `LLMUsage` (нейтральный, замена `AnthropicUsage`): `input_tokens`, `output_tokens`, `model`, `cache_read_tokens`, `cache_write_tokens` (для OpenAI cache_read из `prompt_tokens_details.cached_tokens` если есть, иначе 0; cache_write всегда 0).
- `KeyValidation` (`valid|invalid|offline`) — переиспользуется как есть ([ADR-016](ADR-016-extended-byok-statuses.md)).

### 2. Нормализованный `stop_reason` (канонический словарь)

Внутренний `stop_reason` ∈ **`{tool_use, max_tokens, end_turn}`** — единственные значения, на которые диспетчеризует orchestrator ([ADR-025](ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)). Каждый клиент мапит свой wire stop_reason к каноническому:

| Канонический | Anthropic `stop_reason` | OpenAI `finish_reason` |
|---|---|---|
| `tool_use` | `tool_use` | `tool_calls` |
| `max_tokens` | `max_tokens` | `length` |
| `end_turn` | `end_turn`, `stop_sequence`, прочее | `stop`, `content_filter`, прочее |

Orchestrator сравнивает только с каноническими значениями. **Это снимает текущую завязку** orchestrator на строковые литералы Anthropic (`"tool_use"`/`"max_tokens"`).

### 3. ЦЕНТРАЛЬНОЕ: граница orchestrator ↔ client (провайдер-агностичность персиста)

**Принцип:** вся провайдер-специфичная (де)сериализация wire-формата уходит **ВНУТРЬ клиента**. Orchestrator и слой персиста остаются провайдер-агностичными.

Поскольку **инстанс одно-провайдерный**, формат `chat_steps.payload` = wire-формат активного провайдера. Это допустимо: реплей всегда идёт через тот же клиент, что писал payload. Кросс-провайдерный реплей в одной БД невозможен по построению (инвариант §«Каждый инстанс — ОДИН провайдер»).

**Контракт границы (нормативно для backend):**

1. **Что orchestrator ПЕРЕДАЁТ клиенту** (нейтрально):
   - `system_prompt: str` — как сейчас.
   - `messages` — **нейтральная история шагов**. Рекомендация (минимизация изменений orchestrator): передавать список нейтральных сообщений, который клиент сам переводит в провайдер-messages. Нейтральное сообщение = `{role: user|assistant|tool, content_blocks: [...]}`, где `content_blocks` — это **wire-блоки активного провайдера из `chat_steps.payload`** (для assistant/user) ИЛИ доменная tool-result-запись `{toolCallId, providerToolUseId, toolName, result|error}` (для tool-шага). Клиент строит провайдер-messages из этого.
   - `tools` — **нейтральные определения** (см. §4 ниже): `{name(domain dotted), description, input_schema}` каждого предлагаемого tool. Per-provider сериализацию (имена/обёртку) делает клиент.
   - `attachments: PreparedAttachments | None` — нейтральные вложения первого turn (см. §5). Клиент строит провайдер content-блоки. **Orchestrator больше не строит Anthropic image/document-блоки сам** — это уезжает в клиент.

2. **Что клиент ВОЗВРАЩАЕТ:**
   - `LLMResult.content_blocks` — **wire-формат активного провайдера** для записи в `chat_steps.payload` (Anthropic: content-блоки как сейчас; OpenAI: нормализованный assistant-message `{role:"assistant", content, tool_calls:[...]}` или эквивалент, достаточный для реплея).
   - `LLMResult.tool_uses` — **доменные** `{id(provider raw), name(domain dotted), input(dict)}`. Клиент уже применил reverse-map имени и (для OpenAI) распарсил `arguments` из JSON-строки в dict. Orchestrator получает однородный результат независимо от провайдера.

3. **Что orchestrator ХРАНИТ в `chat_steps`:** `LLMResult.content_blocks` дословно (нормализованные клиентом — см. §6). Реплей (`_build_messages`) собирает нейтральную историю из payload и отдаёт клиенту; **провайдер-специфичную сборку финальных messages делает клиент** (Anthropic: как сейчас; OpenAI: маппинг в Chat Completions messages — assistant с `tool_calls`, role=`tool` с `tool_call_id`).

4. **Инвариант минимизации изменений orchestrator:** orchestrator оперирует только нейтральными типами (`LLMResult`/`LLMUsage`/`NeutralStopReason`) и доменными именами/id. Все строковые литералы Anthropic (`"tool_use"`, `"max_tokens"`, прямая сборка Anthropic-блоков) заменяются на нейтральные. Логика биллинга, барьера хода, server-side tool-loop, idempotency — **не меняется**.

5. **`provider_tool_use_id` ([ADR-008](ADR-008-provider-tool-use-id.md)) обобщается.** Поле хранит raw provider id: Anthropic — `toolu_...`; OpenAI — `call_...` (id из `tool_calls[].id`). Семантика та же: непрозрачная строка провайдера, используется в реплее для согласования tool_use↔tool_result. Имя колонки/поля не меняется (provider-нейтральная семантика). [ADR-024](ADR-024-history-payload-domain-normalization.md) (нормализация истории) работает поверх той же карты `provider_tool_use_id → domain id`.

### 4. Tools: нейтральные определения + per-provider сериализация

- Нейтральное определение tool (single source of truth — `tools.py`, как сейчас): `{name(domain dotted), description, input_schema}`.
- **Per-provider сериализация (внутри клиента):**
  - Anthropic: `{name(underscore), description, input_schema}` — текущий `anthropic_tool_definitions()`.
  - OpenAI: `{type:"function", function:{name(underscore), description, parameters(=input_schema)}}`.
- **Underscore-map переиспользуется.** OpenAI function name тоже `^[a-zA-Z0-9_-]{1,64}$` — **точки запрещены у обоих провайдеров**, поэтому тот же `_DOMAIN_TO_ANTHROPIC`/`to_anthropic_tool_name`/`to_domain_tool_name` (`tools.py`) применяется и для OpenAI (карта переименовывается в нейтральную, но значения/семантика идентичны — dot↔underscore). BUG-3-инвариант сохраняется.
- **Reverse-map при парсинге OpenAI tool_calls:** `tool_calls[].function.name` (underscore) → domain (dotted) через `to_domain_tool_name`; `tool_calls[].function.arguments` — **JSON-строка**, клиент её парсит в dict (невалидный JSON → `ValidationFailedError`, как unmapped tool name). Anthropic отдаёт `input` уже как dict — паритет результата.

### 5. Attachments: провайдер-параметризованный билдер

`prepare_attachments` (`attachments.py`) даёт нейтральный `PreparedAttachments` (валидированные вложения + плейсхолдеры). Построение провайдер content-блоков параметризуется провайдером:
- **Anthropic:** image `{type:image, source:{type:base64,...}}`, document(PDF) `{type:document,...}`, text — как сейчас.
- **OpenAI:** image → `{type:"image_url", image_url:{url:"data:<mediaType>;base64,<data>"}}`; text → текстовый блок; **PDF → `ValidationFailedError` (422 `unsupported_media_type`)** при `provider=openai` (Chat Completions vision не принимает PDF).
- Валидация (allowlist, magic bytes, лимиты, PDF page-guard) **общая, до провайдер-ветвления**. PDF-reject для OpenAI — отдельная провайдер-aware проверка класса `document` ([§5 docs/05-security.md](../05-security.md)).

### 6. Нормализация payload (per-provider)

[ADR-021](ADR-021-deterministic-step-order-and-block-normalization.md) (стрип не-wire SDK-полей перед персистом) — **per-provider**: Anthropic `_BLOCK_WIRE_FIELDS` (как сейчас); OpenAI — собственный allowlist полей assistant-message/tool_calls. Нормализация выполняется **внутри клиента** на границе персиста (orchestrator получает уже чистые `content_blocks`). `seq`-порядок ([ADR-021](ADR-021-deterministic-step-order-and-block-normalization.md)) и барьер хода ([ADR-025](ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)) — провайдер-агностичны, не меняются.

### 7. BYOK per-provider

- `OpenAIClient.validate_key` — лёгкий вызов (минимальный `chat.completions.create` или `models.list`); маппинг `401 → invalid`, network/timeout → `offline`, ok → `valid` (симметрично Anthropic, [ADR-016](ADR-016-extended-byok-statuses.md)).
- BYOK default-модель **per-provider**: `BYOK_DEFAULT_MODEL` (Anthropic, как сейчас) + `OPENAI_BYOK_DEFAULT_MODEL` (OpenAI, дефолт `gpt-4o`). `_active_model_for` (`byok/service.py`) выбирает по активному `LLM_PROVIDER`.
- BYOK на инстансе валидирует ключ **своего** провайдера (пользователь openai-инстанса вводит OpenAI-ключ).

### 8. Factory `get_llm_client()`

Заменяет `get_anthropic_client()` синглтон фабрикой `get_llm_client()` (`src/app/chat/llm_client.py` или `deps.py`): читает `LLM_PROVIDER` → возвращает `AnthropicClient()` (дефолт) или `OpenAIClient()`. Существующий `get_anthropic_client()` сохраняется как тонкая обёртка/алиас на время миграции либо заменяется в `deps.py` (orchestrator + byok инжектят `get_llm_client()`). Тип инъекции в orchestrator/byok меняется `AnthropicClient → LLMClient`.

### 9. Config

Новые env (`config.py`), обратная совместимость `anthropic_*` сохраняется:
- `LLM_PROVIDER` (дефолт `anthropic`).
- `OPENAI_API_KEY` (секрет), `OPENAI_MODEL` (дефолт `gpt-4o`), `OPENAI_MAX_TOKENS` (дефолт `16000`, паритет), `OPENAI_TIMEOUT_SECONDS` (дефолт `120`), `OPENAI_MAX_RETRIES` (дефолт `2`), `OPENAI_BYOK_DEFAULT_MODEL` (дефолт `gpt-4o`).
- `anthropic_*` остаются дефолтами anthropic-инстансов без изменений.

### 10. Observability

- Вводится обобщённая метрика `llm_upstream_errors_total` с label `provider ∈ {anthropic, openai}` (+ существующие `status_code`/`error_type`). **Факт реализации:** legacy-метрика `anthropic_upstream_errors_total` **сохранена ПАРАЛЛЕЛЬНО** с новой `llm_upstream_errors_total{provider}` — обе инкрементируются на anthropic-пути (`anthropic_client.py`), OpenAI-путь (`openai_client.py`) пишет только `llm_upstream_errors_total{provider="openai"}`. Решение: legacy-имя оставлено для обратной совместимости существующих дашбордов/тестов (нет внешнего контракта на имя метрики, только Prometheus scrape — безопасно держать оба временных ряда). Изначально рассматривалось переименование (замена `anthropic_*` → `llm_*`), но при реализации выбран параллельный вариант, чтобы не ломать привязанные к legacy-имени дашборды/тесты.
- OpenAI-ключ (`OPENAI_API_KEY`, BYOK OpenAI-ключ) — под redaction. Уже покрыт денилистом `key`/`secret` (`redaction.py` `_DENY_SUBSTRINGS`), отдельной правки redaction не требуется (`OPENAI_API_KEY` содержит `key`).

### 11. Deployment

3-й инстанс с `LLM_PROVIDER=openai` + `OPENAI_*` per-instance (домен/ключ — позже). `INSTANCES`-loop +1 ([ADR-017](ADR-017-shared-server-traefik-deploy.md)). Дефолт `LLM_PROVIDER=anthropic` → существующие два инстанса не задают переменную (no-op). Детали — [07-deployment.md §Мульти-инстанс](../07-deployment.md#мульти-инстанс--клонирование-сервиса).

## Альтернативы

- **Форк кодовой базы под OpenAI.** Отклонено: дублирование, дрейф, двойная поддержка. Решение пользователя — абстракция в одном коде.
- **Двойной провайдер на инстанс (runtime выбор per-request).** Отклонено: усложняет персист (смешанный wire-формат в одной БД, кросс-провайдерный реплей), не требуется. Один инстанс = один провайдер.
- **OpenAI Responses API вместо Chat Completions.** Отложено ([Q-033-2](../99-open-questions.md)): Chat Completions — зрелый function-calling + vision, прямой паритет с текущим non-streaming Anthropic-путём.
- **Хранить нейтральный (провайдер-агностичный) формат в `chat_steps.payload`.** Отклонено на MVP: добавляет двунаправленную трансляцию neutral↔wire на каждый реплей без выгоды (инстанс одно-провайдерный). Wire-формат активного провайдера достаточен. Если потом понадобится кросс-провайдерный реплей в одной БД — отдельный ADR ([Q-033-1](../99-open-questions.md)).

## Последствия

- **Положительные:** OpenAI-клон разворачивается тем же кодом/деплоем; существующие инстансы не затронуты (дефолт anthropic); orchestrator/персист становятся провайдер-агностичными; tools/attachments/BYOK переиспользуют существующую логику с тонким провайдер-ветвлением.
- **Цена:** новый OpenAI SDK в стеке; per-provider ветки в client/attachments/byok; `chat_steps.payload` несёт wire-формат провайдера (нет кросс-провайдерного реплея — это инвариант, не дефект).
- **Tech debt:** PDF на OpenAI отключён ([TD-023](../100-known-tech-debt.md)) *(снято [ADR-041](ADR-041-openai-native-pdf-attachment.md), TD-023 Resolved 2026-06-19)*; OpenAI Responses API ([Q-033-2](../99-open-questions.md)); граница persist-формата = wire активного провайдера ([Q-033-1](../99-open-questions.md)).
- **Безопасность:** OpenAI-ключи под redaction; PDF-reject для openai — чистый 422, не 500; модель угроз вложений/preview не меняется.
