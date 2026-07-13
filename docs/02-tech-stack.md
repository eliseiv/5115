# 02 — Tech Stack

Единственное место, где фиксируются язык, библиотеки, версии и команды. Все остальные агенты берут команды отсюда. Если чего-то нет здесь — это блокер, не угадывай.

## Язык и runtime
| Компонент | Версия | Примечание |
|---|---|---|
| Python | **3.12.x** (пин **`>=3.12,<3.13`**) | целевой `3.12`, CI на `3.12`. Верхняя граница закреплена явно — см. блок «Пин Python 3.12» ниже. |
| FastAPI | **0.115.x** | ASGI framework |
| Uvicorn | **0.32.x** (`uvicorn[standard]`) | ASGI server (dev); в prod за Gunicorn |
| Gunicorn | **23.x** | process manager, worker class `uvicorn.workers.UvicornWorker` |
| Pydantic | **2.9.x** | v2, строгая валидация схем |
| pydantic-settings | **2.5.x** | конфиг из env |

> **Пин Python 3.12 (воспроизводимость сборки; спринт 1).** `pyproject.toml` фиксирует **`requires-python = ">=3.12,<3.13"`** и репозиторий несёт **`.python-version` = `3.12`**. Причина: без верхней границы на чистой машине uv выбирает новейший интерпретатор (наблюдалось **3.14**), под который отсутствуют бинарные колёса зафиксированных зависимостей (в первую очередь `asyncpg>=0.29,<0.30` — есть `cp312`-колёса, нет `cp314`), и `uv sync` падает на сборке из исходников. Свидетельство в истории — коммит `a30aa8c` («ci: re-trigger (transient uv sync/cache failure)»). **Прод не был затронут** (Dockerfile пинит `python:3.12-slim-bookworm`); страдали dev и CI. **Решение (принято спринтом 1):** явный пин `<3.13` детерминированно направляет uv на 3.12.x, где `cp312`-колёса всех зафиксированных зависимостей доступны, а `uv.lock` фиксирует их версии — dev/CI/prod выровнены на 3.12. **Остаточного долга нет** (TD не заводится): пин интерпретатора + `uv.lock` + `cp312`-колёса дают воспроизводимую сборку без сборки из исходников; при появлении зависимости без `cp312`-колеса или намерении поднять минорную версию Python — это отдельное решение с обновлением этого блока. См. также [07-deployment.md §Артефакт](07-deployment.md#артефакт).

## Данные
| Компонент | Версия | Примечание |
|---|---|---|
| PostgreSQL | **16.x** | единственное хранилище состояния |
| SQLAlchemy | **2.0.x** | async ORM (`AsyncSession`) |
| asyncpg | **0.29.x** | async драйвер PostgreSQL |
| Alembic | **1.13.x** | миграции |
| Redis | **7.x** | rate limiting, idempotency-метки, policy cache |
| redis-py | **5.x** | async client (`redis.asyncio`) |

## Внешние SDK / интеграции
| Компонент | Версия | Назначение |
|---|---|---|
| openai | **>=1.99,<2** (Python SDK; зарезолвлено **`1.109.1`** в `uv.lock`) | **OpenAI Responses API** (`client.responses.create`; function-calling + vision + встроенный веб-поиск + reasoning, non-streaming) — провайдер-абстракция LLM ([ADR-033](adr/ADR-033-llm-provider-abstraction.md), Responses с [ADR-059](adr/ADR-059-openai-default-provider.md)). **Переход с Chat Completions на Responses** ([ADR-059](adr/ADR-059-openai-default-provider.md), закрывает [Q-033-2](99-open-questions.md)): единственный путь режима Search (веб-поиск GA-tool `web_search` только в Responses) и Deep Thinking (`reasoning.effort` + реплей `include=["reasoning.encrypted_content"]`+`store=False`). Плоский `FunctionToolParam` (`{type,name,parameters,strict}`), `max_output_tokens`, `input` items, `provider_tool_use_id`=`call_id`. Подключается при `LLM_PROVIDER=openai`; на anthropic-инстансах (кодовый дефолт) в рантайме не используется, но зависимость общая (один код). Async-клиент `openai.AsyncOpenAI`; per-call key override (BYOK). Вложения на Responses — `input_image`/`input_file` (пересматривает wire [ADR-041](adr/ADR-041-openai-native-pdf-attachment.md)). Констрейнт **`<2`** — мажор может ломать tool-loop/usage-parsing. `anthropic` не трогается. Генерация изображений — **`gpt-image-1`** через отдельный `AsyncOpenAI`-клиент `images.generate` ([ADR-058](adr/ADR-058-image-generation.md); `response_format` gpt-image-1 отвергает → всегда base64). |
| anthropic | **0.39.x** (Python SDK) | Claude messages API + prompt caching. ⚠️ **Не типизирует `document`-блок** (есть `ImageBlockParam`, нет `DocumentBlockParam` — проверено эмпирически). Backend передаёт `messages` как сырые dict (`cast(Any, ...)`), поэтому PDF-`document`-блок проходит без отказа SDK; wire-совместимость для `claude-sonnet-4-5/4-6` подтверждается e2e ([ADR-020](adr/ADR-020-inline-base64-attachments-mvp.md)). Bump SDK для типобезопасности document-блока — [TD-016](100-known-tech-debt.md). |
| httpx | **0.27.x** | App Store Server API, прочие HTTP |
| pyjwt | **2.9.x** | JWT verify (или App Store JWS) |
| cryptography | **43.x** | AES-GCM envelope encryption (data layer); StoreKit JWS x5c chain/signature verify |

> **Admin-auth и preview signed URL новых внешних зависимостей не требуют.** HMAC-SHA256 для preview signed URL и
> constant-time сравнение admin/preview-токенов — stdlib `hmac`/`hashlib`/`secrets` (Python 3.12). `cryptography` (уже в стеке)
> при необходимости. См. [ADR-009](adr/ADR-009-admin-token-auth.md), [ADR-010](adr/ADR-010-backend-hosted-preview.md).
| prometheus-client | **0.21.x** (`>=0.21,<0.22`) | Prometheus exposition для `GET /metrics` (observability cross-cutting) |
| pypdf | **5.x** (`>=5,<6`; зарезолвлено `5.9.0` в `uv.lock`) | **MVP ([ADR-020](adr/ADR-020-inline-base64-attachments-mvp.md)): guard числа страниц PDF** (анти-decompression-bomb) — только подсчёт страниц/структуры, без полного рендера (`src/app/chat/attachments.py::_check_pdf_pages`). PDF отдаётся Claude нативным `document`-блоком, текст НЕ извлекается (Claude разбирает сам). Pure-Python, без системных зависимостей. Фактически добавлен в `pyproject.toml`/`uv.lock` при реализации ADR-020 (2026-06-03); до этого числился в стеке только в docs (см. примечание ниже). (Извлечение `extracted_text` по [ADR-014](adr/ADR-014-multimodal-attachments.md) — отложено вместе с двухшаговой моделью, [TD-015](100-known-tech-debt.md).) |
| python-multipart | **0.0.x** (актуальная) | парсинг `multipart/form-data`. **Для MVP-вложений ([ADR-020](adr/ADR-020-inline-base64-attachments-mvp.md)) НЕ требуется** (inline base64 в JSON, без multipart-upload). Остаётся в стеке (был добавлен под двухшаговый `POST /v1/attachments` [ADR-014](adr/ADR-014-multimodal-attachments.md) → [TD-015](100-known-tech-debt.md)); удаление — отдельное решение, безвредно. |

> **MVP-вложения (2026-06-03, [ADR-020](adr/ADR-020-inline-base64-attachments-mvp.md)):** мультимодальный ввод реализуется inline base64 в `/v1/chat/run` (base64 — stdlib; magic-bytes — stdlib-сигнатуры). Единственная требующаяся внешняя зависимость — `pypdf` (PDF page-guard). Важно: `pypdf` числился в стеке (в этом документе) уже с расширения Figma-gap 2026-06-02, но **физически в `pyproject.toml`/`uv.lock` отсутствовал** — реальная зависимость `pypdf>=5,<6` добавлена backend'ом именно при реализации ADR-020 (2026-06-03). Пин в коде совпадает с задокументированным. Прочих новых внешних зависимостей фича не добавляет. Новые config-настройки (env, конфигурируемы, [Q-020-2](99-open-questions.md)): `ATTACHMENT_MAX_COUNT` (дефолт 10), `ATTACHMENT_MAX_BYTES_IMAGE` (5 MB) и `ATTACHMENT_MAX_BYTES_DOCUMENT` (8 MB — отдельные env-ключи в коде, `src/app/config.py`), `ATTACHMENT_TOTAL_BYTES` (10 MB), `ATTACHMENT_PDF_MAX_PAGES` (100), `ATTACHMENT_REQUEST_BODY_LIMIT` (12 MB — повышенный transport-лимит только роута `/v1/chat/run`). Allowlist `mediaType` — фиксирован в коде ([Q-020-1](99-open-questions.md) — расширение).

> **Расширение Figma-gap (2026-06-02):** в стек (на уровне этого документа) были внесены `pypdf` (тогда — под извлечение `extracted_text` из PDF, модуль attachments) и `python-multipart` (multipart-upload вложений). Уточнение (2026-06-03): на тот момент это была только docs-фиксация намерения — `pypdf` физически в `pyproject.toml` отсутствовал и был реально добавлен лишь при реализации [ADR-020](adr/ADR-020-inline-base64-attachments-mvp.md) (с назначением «guard числа страниц», а не извлечение текста — извлечение отложено в [TD-015](100-known-tech-debt.md)). Прочие новые модули (chats/profile/preferences/workspaces/snippets/token-purchase/notifications) **новых внешних зависимостей не требуют** — используют существующий стек (FastAPI/SQLAlchemy/Pydantic, stdlib `hmac`/`hashlib`/`secrets` для accountId-производной). Определение media_type вложений по magic bytes — stdlib (`mimetypes`/собственная сигнатурная проверка), без новой зависимости. APNs-отправка push отложена ([TD-011](100-known-tech-debt.md)) — APNs-зависимость добавится при её реализации, не в этом проходе.

> **Adapty subscription webhook ([ADR-029](adr/ADR-029-adapty-subscription-webhook.md)) — новых внешних зависимостей не требует.** Bearer constant-time compare — stdlib `hmac.compare_digest` (как admin/preview). Дефенсивный парсинг тела — stdlib `json` (сырое тело `request.body()`, без Pydantic-валидации тела — иначе `422` на проверочный пинг Adapty). UUID-разбор `customer_user_id` — stdlib `uuid`. ISO8601 `expires_at` — stdlib `datetime`. Идемпотентность — PostgreSQL `ON CONFLICT` (уже в стеке). Новые config-настройки (env, `src/app/config.py`): `ADAPTY_WEBHOOK_SECRET` (секрет, дефолт пуст → эндпоинт `500`), `ADAPTY_PRODUCT_TOKENS` (JSON `vendor_product_id→tokens`, дефолт `{}`, хелпер `adapty_product_tokens()` по образцу `token_products()`), `ADAPTY_SUBSCRIPTION_TOKENS_GRANT` (int, дефолт `1000`, fallback-грант). Точные значения/назначение — [07-deployment.md §Конфигурация (env)](07-deployment.md#конфигурация-env), [modules/billing-adapty/07-implementation-phases.md](modules/billing-adapty/07-implementation-phases.md).

> **Дата/время — stdlib `datetime` + `zoneinfo` (инструмент `time.now`, [ADR-026](adr/ADR-026-global-server-side-tools-and-time-now.md)).** UTC-набор результата (`utc`/`unix`/`weekday`) — чистый stdlib `datetime` от `datetime.UTC`, без внешних зависимостей. Локальное время по IANA-зоне (`tz` → `local`/`timezone`) — stdlib `zoneinfo` (`ZoneInfo`), которому нужна **база таймзон**. Базовый образ `python:3.12-slim-bookworm` ([Dockerfile](Dockerfile)) — slim; системная tz-база может отсутствовать. Пакет **`tzdata`** добавлен в зависимости проекта (`pyproject.toml` — `tzdata>=2024.1`; `uv.lock` — `tzdata 2026.2`; pure-Python, предпочтительно для slim, без правки Dockerfile) → в prod-образе tz-база гарантирована, `ZoneInfo("Europe/Moscow")` резолвится, `tz` работает — **[TD-019](100-known-tech-debt.md) Resolved (2026-06-10, вариант A)**. UTC-набор доступен всегда независимо; невалидная/мусорная зона штатно деградирует к tool-result error `invalid_timezone`. Время в `time.now` берётся через инъектируемый `Clock`-провайдер (детерминизм qa, [ADR-026 §8](adr/ADR-026-global-server-side-tools-and-time-now.md)), не прямой `datetime.now()`.

> **Смена дефолтного провайдера ([ADR-059](adr/ADR-059-openai-default-provider.md)).** Новые режимы диалога ([ADR-055](adr/ADR-055-dialog-mode.md)) требуют OpenAI Responses API — целевой дефолт продукта = OpenAI. **Дефолт меняется через env инстанса (`LLM_PROVIDER=openai` + `OPENAI_API_KEY`), НЕ сменой кодового дефолта** — иначе anthropic-инстансы, полагающиеся на дефолт, молча переключатся и упадут. **Существующие anthropic-инстансы ОБЯЗАНЫ явно задать `LLM_PROVIDER=anthropic`** (проверяемое допущение, [07-deployment.md](07-deployment.md#конфигурация-env)). Anthropic остаётся рабочим для BYOK/совместимости, но без новых режимов ([TD-030](100-known-tech-debt.md)).
>
> **Провайдер-абстракция LLM ([ADR-033](adr/ADR-033-llm-provider-abstraction.md)).** LLM-клиент выбирается env **`LLM_PROVIDER ∈ {anthropic, openai}`, кодовый дефолт `anthropic`** (существующие инстансы `claude-ios`/`avelyra` поведение не меняют). Нейтральный интерфейс `LLMClient` (`src/app/chat/llm_client.py`) — контракт `create_message`/`validate_key`; `AnthropicClient` (как есть) и новый `OpenAIClient` — его реализации; factory `get_llm_client()`. **Один провайдер на инстанс** — БД хранит wire-формат своего провайдера (кросс-провайдерный реплей в одной БД не требуется). Нормализованный внутренний `stop_reason ∈ {tool_use, max_tokens, end_turn}` (каждый клиент мапит свой wire: OpenAI `tool_calls→tool_use`, `length→max_tokens`, `stop→end_turn`). Провайдер-специфичная (де)сериализация — **внутри клиента**; orchestrator/персист провайдер-агностичны. Контракт границы, маппинги имён tools, attachments — [chat-orchestrator/03-architecture.md §Провайдер-абстракция LLM](modules/chat-orchestrator/03-architecture.md#провайдер-абстракция-llm-anthropic--openai-adr-033). Config-имена (`OPENAI_*`) — [07-deployment.md §Конфигурация (env)](07-deployment.md#конфигурация-env).

### Модели Claude (значения по умолчанию)
- Оркестрация: `claude-sonnet-4-5` (значение конфигурируемо через env `ANTHROPIC_MODEL`).
- Prompt caching: включён через `cache_control` на системном промте и стабильном контексте.
- Точную модель/версию pin фиксирует Chat Orchestrator config; пользователь BYOK использует ту же модель, но со своим ключом.
- **`ANTHROPIC_MAX_TOKENS` (output budget на вызов, [ADR-025](adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)):** дефолт **`16000`** (`src/app/config.py`). Прежний дефолт `4096` был мал для генерации кода/файлов (несколько `files.write` с полным содержимым) → ответ обрезался (`stop_reason="max_tokens"`). `16000` покрывает типовой генеративный ход; вызов остаётся **non-streaming** (`create_message`) — `16000` ниже порога SDK non-streaming-гарда. **Per-instance** в `.env`-контракте (применяется к каждому инстансу мульти-инстанс-деплоя, [ADR-017](adr/ADR-017-shared-server-traefik-deploy.md)). Обрезку по `max_tokens` оркестратор обрабатывает явно (`status=blocked`, `blockReason=max_tokens`, кредит не списывается) — [chat-orchestrator/03-architecture.md §Обработка обрезки](modules/chat-orchestrator/03-architecture.md#обработка-обрезки-по-max_tokens-adr-025). Переход на streaming при дальнейшем росте `max_tokens` — [TD-018](100-known-tech-debt.md).
- **`ANTHROPIC_TIMEOUT_SECONDS`:** дефолт поднят до **`120`** (с 60) — страховка от ложного `502` по таймауту на длинной non-streaming-генерации при `max_tokens=16000` ([ADR-025](adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)). Конфигурируемо, существенно ниже SDK non-streaming-гарда.
- `ANTHROPIC_MAX_RETRIES` (дефолт `2`) — без изменений.

### Модель OpenAI (инстансы `LLM_PROVIDER=openai`, [ADR-033](adr/ADR-033-llm-provider-abstraction.md), [ADR-059](adr/ADR-059-openai-default-provider.md))
- **Responses API** (`client.responses.create`, [ADR-059](adr/ADR-059-openai-default-provider.md)), non-streaming. Заменяет прежний Chat Completions-путь (нужен для Search/Deep Thinking/квизов).
- Оркестрация: **`gpt-4o`** (env `OPENAI_MODEL`).
- **Deep Thinking** ([ADR-055](adr/ADR-055-dialog-mode.md)/[ADR-059 §4](adr/ADR-059-openai-default-provider.md)): форсит reasoning-модель `DEEP_THINKING_MODEL` (переопределяет выбор пользователя и stale-guard, включая BYOK), `reasoning={"effort": DEEP_THINKING_EFFORT}`. Env: `DEEP_THINKING_MODEL`/`DEEP_THINKING_EFFORT` (`low|medium|high`)/`DEEP_THINKING_TIMEOUT_SECONDS`.
- **Search** ([ADR-055](adr/ADR-055-dialog-mode.md)/[ADR-059 §2](adr/ADR-059-openai-default-provider.md)): встроенный tool GA `web_search` (не legacy preview), `search_context_size` = `OPENAI_SEARCH_CONTEXT_SIZE`; цитаты `url_citation`.
- **Генерация изображений** ([ADR-058](adr/ADR-058-image-generation.md)): `gpt-image-1` через отдельный `AsyncOpenAI`-клиент `images.generate` (свои таймауты/ретраи `IMAGE_TIMEOUT_SECONDS`/`IMAGE_MAX_RETRIES`; `IMAGE_MODEL`/`IMAGE_DEFAULT_SIZE`/`IMAGE_DEFAULT_QUALITY`). `response_format` для `gpt-image-1` **не передаётся** (результат всегда base64 → `b64decode`).
- `OPENAI_MAX_TOKENS` (output-бюджет, дефолт `16000` — паритет), `OPENAI_TIMEOUT_SECONDS` (дефолт `120`), `OPENAI_MAX_RETRIES` (дефолт `2`).
  - **Параметр Chat Completions:** `OPENAI_MAX_TOKENS` передаётся в Chat Completions как `max_tokens` — валидный параметр для `gpt-4o` (MVP-модель). При переходе на reasoning-модели (семейство `o*`/новые модели, требующие `max_completion_tokens`) этот параметр в API называется `max_completion_tokens`; для текущей MVP-модели `gpt-4o` `max_tokens` корректен. Смена — отдельным решением при выборе reasoning-модели.
- BYOK активная модель OpenAI: `OPENAI_BYOK_DEFAULT_MODEL` (дефолт `gpt-4o`) — отдельно от anthropic `BYOK_DEFAULT_MODEL`.
- Кэширование: `cache_control` не применяется (Anthropic-only); у OpenAI авто-кэш промпт-префикса, спец-логики в коде нет. `usage.cache_read_tokens` берётся из `prompt_tokens_details.cached_tokens` (если есть), `cache_write_tokens` = 0.

## Инструменты разработки
| Инструмент | Версия | Роль |
|---|---|---|
| uv | **0.4.30** (CI pin) | менеджер зависимостей и venv (`uv.lock`). CI запинен на `0.4.30` (`.github/workflows/ci.yml`, `UV_VERSION`); локальное dev-окружение может быть новее (наблюдалось `0.11.6`) — `uv.lock` обеспечивает идентичность зависимостей независимо от версии uv. Каноничен CI-pin `0.4.30`. |
| Ruff | **0.7.x** | linter + formatter |
| mypy | **1.11.x** | статическая типизация (strict для бизнес-логики) |
| pytest | **8.x** | тесты |
| pytest-asyncio | **0.24.x** | async тесты |
| pytest-cov | **5.x** | coverage |
| testcontainers | **4.x** | реальный PostgreSQL/Redis в интеграционных тестах |
| respx | **0.21.x** | мок HTTP (Anthropic/Apple) в unit-тестах |

## Команды (канонические — использовать именно их)
> Запускаются из корня репозитория. Менеджер — `uv`.

```bash
# install deps
uv sync

# format (пишет изменения)
uv run ruff format .

# format check (CI, без записи)
uv run ruff format --check .

# lint
uv run ruff check .

# lint с автофиксом
uv run ruff check --fix .

# type-check
uv run mypy src

# тесты + покрытие
uv run pytest --cov=src --cov-report=term-missing --cov-fail-under=80

# миграции
uv run alembic upgrade head
uv run alembic revision --autogenerate -m "<message>"

# запуск приложения (dev)
uv run uvicorn app.main:app --reload

# запуск (prod)
uv run gunicorn app.main:app -k uvicorn.workers.UvicornWorker -w 4
```

## Структура проекта (фактическая)
```
src/app/
  main.py                 # FastAPI app factory: middleware, routers, exception handlers
  config.py               # pydantic-settings (Settings, get_settings)
  db.py                   # async engine, sessionmaker, dispose_engine
  deps.py                 # FastAPI dependencies (auth/JWT, db session, correlation id)
  errors.py               # AppError иерархия (ValidationFailedError и пр.)
  api_gateway/
    auth.py               # JWT (RS256) verify, JWKS; require_admin (X-Admin-Token, изолирован от get_current_user — ADR-009)
    middleware.py         # CorrelationId, SecurityHeaders, SizeLimit
    rate_limit.py         # Redis rate limiting, redis_ping, close_redis
    routers/              # chat, policy, wallet, subscription, byok, health, admin (/v1/admin/*), preview (/v1/preview/*)
  chat/                   # orchestrator, anthropic_client, repository, tools (server-side site.* dispatch + global time.now/quiz.generate/image.generate; client-side инструменты удалены — ADR-063)
  admin/                  # service-обёртка над Wallet.grant/get_wallet_view (ADR-009)
  website/                # projects/site_files service, signed URL (HMAC), site.* tool-хэндлеры (server-side, ADR-010/011)
  policy/                 # engine (pure), loader (state из репозиториев)
  wallet/                 # service (ledger, consume, grant)
  subscription/           # service, storekit (JWS x5c chain/signature verify)
  byok/                   # service (envelope encrypt/toggle/delete), kms (KmsClient + LocalKmsClient)
  audit/                  # service (append-only logging)
  observability/          # metrics (prometheus), logging, context (request id), redaction
  models/                 # base, tables. На MVP активны 14 таблиц (миграции 0001=9 базовых, 0003=projects/site_files, 0004=user_preferences, 0005=auth_devices/auth_refresh_tokens). Поставка 3 (миграция 0011, ADR-036): workspace_projects + workspace_files (BYTEA, самодостаточно, НЕ через attachments) + chat_sessions.workspace_project_id. Спроектированы, миграцией ещё не созданы: snippets/device_push_tokens; ОТЛОЖЕНЫ: attachments (TD-015, ADR-020). Полный статус — 03-data-model.md.
  schemas/                # Pydantic request/response (chat, policy, wallet, subscription, byok, common)
  # Расширение Figma-gap (новые пакеты — каждый со своим router/service/repository):
  chats/                  # CRUD/список/поиск/steps-view поверх chat_sessions (модуль chats)
  profile/                # displayName + производный accountId
  preferences/            # default_assistant_mode, notif toggle, code defaults
  workspaces/             # рабочие пространства чатов (≠ website projects, ADR-013)
  snippets/               # сохранённые код-фрагменты
  attachments/            # двухшаговый upload/extract_text/таблица — ОТЛОЖЕН (TD-015, transport ADR-014 Superseded); MVP — inline base64 в /chat/run (ADR-020, реализует chat-orchestrator)
  token_purchase/         # consumable IAP → grant кредитов (reuse storekit verifier + Wallet, ADR-015)
  notifications/          # device push-token CRUD (отправка push → TD-011)
migrations/               # alembic
tests/
  unit/  integration/  e2e/
pyproject.toml  uv.lock
```

> Служебные маршруты `/health`, `/healthz` (алиас `/health`, [ADR-017](adr/ADR-017-shared-server-traefik-deploy.md)), `/ready`, `/metrics` — в `api_gateway/routers/health.py` (см. [07-deployment.md](07-deployment.md#health--readiness)).

## Соглашения по коду
- Форматирование и линт — Ruff (line length 100). Конфиг в `pyproject.toml`.
- mypy `strict = true` для пакетов `policy`, `wallet`, `byok`, `chat`. Остальное — `disallow_untyped_defs`.
- Pydantic v2 модели для всех request/response; запрет «голых» dict на границе API.
- Async везде, где есть I/O (БД, Redis, HTTP).
- Идемпотентность через ключи в Redis + уникальные индексы в PostgreSQL (не полагаться только на одно из двух).
