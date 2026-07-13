# 09 — E2E-тестирование в контейнерах (docker-compose)

Цель: поднять backend в контейнерах (`docker-compose.yml`: postgres + redis + migrate + api) и
прогнать полный набор сквозных сценариев против **живого** сервиса, подтвердив реализацию всех
бизнес-правил §[00-vision](00-vision.md) и Acceptance Criteria. E2E дополняет (не заменяет)
unit/integration-пирамиду из [06-testing-strategy.md](06-testing-strategy.md).

Этот документ — источник истины для e2e-прогона. Scope для backend (что доработать перед e2e) и
для qa (что прогнать) — в конце.

---

## 1. Стратегия внешних интеграций в e2e

| Внешний сервис | Режим в e2e | Обоснование |
|---|---|---|
| **Anthropic (Claude)** | **Реальный** API через `ANTHROPIC_API_KEY` | Генерация и tool-loop тестируются против живого Claude. Никаких изменений кода — только конфигурация. |
| **StoreKit / App Store** | **Test-mode** (`STOREKIT_TEST_MODE=true`) | Реальная Apple-подписанная JWS-транзакция и Apple root CA недоступны в e2e. Env-gated test-mode принимает контролируемую тестовую транзакцию. Зарегистрировано как [TD-007](100-known-tech-debt.md). |
| **KMS (BYOK envelope)** | `LocalKmsClient` (реальный AES-256-GCM) | Уже реализованный дефолт для local/CI ([05-security.md](05-security.md)). Не stub — настоящее шифрование под `KMS_LOCAL_MASTER_KEY`. |
| **PostgreSQL 16, Redis 7** | **Реальные** контейнеры | По [06-testing-strategy.md](06-testing-strategy.md): БД и Redis не мокаются. |
| **JWT-издатель** | Локальная RS256-пара (test JWKS / статичный публичный ключ через `JWT_*`) | Токены подписываются тестовым приватным ключом; сервис верифицирует публичным. Прод-поведение проверки не меняется. |

### 1.1 Anthropic в e2e
- Используется реальный `ANTHROPIC_API_KEY` (предоставляет пользователь), передаётся через env.
- Модель e2e: `ANTHROPIC_MODEL` (дефолт `claude-sonnet-4-5`, см. `src/app/config.py`). Для tool-loop
  модель **обязана** уметь вызывать наши tools — определения берутся из уже реализованного
  `anthropic_tool_definitions` (см. [chat-orchestrator/03-architecture.md](modules/chat-orchestrator/03-architecture.md)).
- Изменений кода не требуется. tool-loop-сценарии — единственные, кому нужен живой Claude
  (плюс BYOK `set`-валидация, которая делает лёгкий реальный вызов Anthropic).

---

## 2. STOREKIT_TEST_MODE — env-gated режим тестовой верификации

Требование (реализует backend, регистрируется как [TD-007](100-known-tech-debt.md)).
Семантика и контракт продублированы в [modules/subscription/03-architecture.md](modules/subscription/03-architecture.md#test-mode-верификации-storekit_test_mode)
и [modules/subscription/02-api-contracts.md](modules/subscription/02-api-contracts.md).

### 2.1 Поведение по флагу
- **`STOREKIT_TEST_MODE=false` (дефолт, prod): поведение НЕ меняется.** `StoreKitVerifier.verify`
  выполняет реальную JWS-верификацию: разбор `x5c`, проверка цепочки до Apple root CA из
  `APPSTORE_ROOT_CERT_DIR`, ES256-подпись лифом, проверка `bundleId`/`environment`. Без
  настроенного root CA — **fail-closed (422)**, как сейчас. Никакого test-payload в prod не
  принимается.
- **`STOREKIT_TEST_MODE=true` (только e2e/CI): дополнительная ветка** в `verify`, которая принимает
  контролируемую тестовую транзакцию **до** обращения к реальной JWS-цепочке. Реальная ветка
  остаётся доступной как fallback (если payload не распознан как тестовый — идёт обычная JWS-логика).

### 2.2 Что считается валидной тестовой транзакцией
Тестовая транзакция — компактный **JWS, подписанный HS256** общим секретом `STOREKIT_TEST_SECRET`
(env, обязателен при `STOREKIT_TEST_MODE=true`; пусто → test-mode не активируется даже при флаге).
Выбор HS256 + shared secret: воспроизводимо, не требует Apple-инфраструктуры, и payload остаётся
криптографически защищён от подделки клиентом (нельзя сгенерировать без секрета).

Распознавание тестовой транзакции (в порядке):
1. `STOREKIT_TEST_MODE=true` и `STOREKIT_TEST_SECRET` непуст.
2. JWS-заголовок содержит `alg=HS256` (а не `ES256` с `x5c`) — однозначный признак тестового пути.
3. Подпись валидна под `STOREKIT_TEST_SECRET`. Невалидная подпись → `ValidationFailedError` → `422`
   (поддельная тестовая транзакция отклоняется так же, как поддельная реальная).

Если `alg=ES256`/`x5c` присутствует — идёт **реальная** JWS-ветка независимо от флага (тестовый
режим не ослабляет проверку настоящих Apple-транзакций).

### 2.3 Извлечение нормализованных полей (тот же `VerifiedTransaction`)
Из payload тестового JWS (поля совпадают с App Store-семантикой):
- `transactionId` (обязателен) → `transaction_id` (ключ идемпотентности grant: `sub-grant:{transactionId}`).
- `originalTransactionId` (опц., дефолт = `transactionId`) → `original_transaction_id`.
- `productId` → `product_id` (становится `plan`).
- `expiresDate` (epoch ms) → `expires_at` (UTC). `expires_at > now()` и не revoked → `active`.
- `revocationDate` (опц.) → `revoked=true` → `status=expired`.
- `environment` → нормализуется к нижнему регистру (для test-mode допустимо `"sandbox"`).
- `bundleId` сверяется с `APPSTORE_BUNDLE_ID`, если он задан (как в реальной ветке).

Дальше — **без изменений**: `SubscriptionService.sync` upsert'ит `subscriptions`, при `active`
вызывает `Wallet.grant(SUBSCRIPTION_CREDITS_PER_PERIOD)` идемпотентно по `transactionId`
([ADR-006](adr/ADR-006-credit-billing-and-subscription-grant.md)), пишет audit `subscription_change`.
Это позволяет прогнать активацию подписки + начисление кредитов end-to-end.

### 2.4 Безопасность test-mode (защита от случайного включения в prod)
- Флаг **по умолчанию `false`** → prod fail-closed сохраняется без конфигурации.
- При старте приложения, если `STOREKIT_TEST_MODE=true` → **WARNING в лог** на старте:
  `"STOREKIT_TEST_MODE is ENABLED — accepting HS256 test transactions. MUST be false in production."`
- Test-mode активен **только** при одновременном `STOREKIT_TEST_MODE=true` **и** непустом
  `STOREKIT_TEST_SECRET`. Один флаг без секрета test-mode не включает.
- `STOREKIT_TEST_SECRET` — секрет (env / secret manager), под redaction-allowlist
  ([05-security.md](05-security.md)); тестовый JWS-payload не логируется, как и реальный.
- Реальная JWS-ветка для `ES256`/`x5c`-транзакций работает всегда, флаг её не ослабляет.

---

## 3. Предусловия e2e (must-fix перед прогоном)

### 3.1 [TD-008] migrations/env.py — sqlalchemy.url из Alembic Config
> Статус: **выполнено** ([TD-008](100-known-tech-debt.md) закрыт; migrate-job в e2e прошёл, миграция 0002 применена). Раздел сохранён как исходный scope-контекст прогона.

`migrations/env.py` ранее брал URL из `get_settings().database_url` (функция `_db_url()`), а не из
`context.config` (`alembic.ini` → `sqlalchemy.url`). Это создаёт хрупкость порядка тестов и мешает
запускать миграции против произвольной БД, переданной через Alembic Config (например, отдельная
e2e-БД или testcontainers-инстанс).

**Требование:** `migrations/env.py` должен брать `sqlalchemy.url` из переданного Alembic
`context.config` (с fallback на `get_settings().database_url` только если ключ пуст/не задан, чтобы
не сломать текущий docker-compose `migrate`-job, который полагается на `DATABASE_URL`). Порядок:
1. `config.get_main_option("sqlalchemy.url")` или значение из `config.get_section(...)`, если оно непусто;
2. иначе — `get_settings().database_url`.

Это must-fix перед e2e (надёжность и предсказуемость миграций).

### 3.2 Конфигурация e2e (env)
Минимальный набор для контейнерного прогона (через `.env`, не коммитится):

| Переменная | Значение в e2e |
|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://postgres:postgres@postgres:5432/claude_ios` (compose default) |
| `REDIS_URL` | `redis://redis:6379/0` (compose default) |
| `ANTHROPIC_API_KEY` | **реальный ключ** (пользователь) |
| `ANTHROPIC_MODEL` | `claude-sonnet-4-5` (или согласованная tool-capable модель) |
| `STOREKIT_TEST_MODE` | `true` |
| `STOREKIT_TEST_SECRET` | тестовый общий секрет (генерируется для прогона) |
| `SUBSCRIPTION_CREDITS_PER_PERIOD` | `1000` (дефолт, ADR-006) |
| `KMS_LOCAL_MASTER_KEY` | тестовый master key (LocalKmsClient) |
| `JWT_JWKS_URL` / `JWT_ISSUER` / `JWT_AUDIENCE` | указывают на тестовый RS256-издатель/JWKS |
| `DOCS_ENABLED` | `true` (для сценария Swagger) |
| `TRUSTED_PROXY_IPS` | значение, при котором per-IP rate limit достижим в e2e-сети |

Healthcheck `api` ждёт `GET /ready` (db+redis) — e2e начинается после `service_healthy`.

### 3.3 Процедура подъёма (bring-up)
Фактический bring-up для e2e использует базовый compose + e2e-override:

```
docker compose -f docker-compose.yml -f docker-compose.e2e.yml up -d
```

- **`docker-compose.e2e.yml`** — отдельный e2e-артефакт, **не меняет** base `docker-compose.yml`. Он лишь снимает публикацию хост-портов `postgres` (5432) и `redis` (6379) через `ports: !reset []`, потому что на части хостов нативные PostgreSQL/Redis уже занимают эти порты. `api`/`migrate` обращаются к `postgres`/`redis` по имени сервиса во внутренней compose-сети, публикация хост-портов им не нужна. `api` сохраняет `127.0.0.1:8000` для доступа qa/smoke.
- **Требуется Docker Compose v2.24+** — синтаксис `!reset []` (очистка унаследованного списка `ports`) появился в v2.24. Минимальная версия compose согласована с [07-deployment.md](07-deployment.md#локальный-подъём-и-e2e-override).
- Старт e2e — после `api` → `service_healthy` (см. §3.2).

---

## 4. E2E-сценарии (Acceptance для прогона)

Каждый сценарий — против живого `api` в контейнере. Колонка «Зависимость»: `Claude` = нужен живой
Anthropic; `StoreKit-test` = нужен `STOREKIT_TEST_MODE`; `—` = независим (только БД/Redis/JWT).

### 4.1 Subscription + grant (StoreKit-test)
| ID | Сценарий | Ожидание | Зависимость | AC/BR |
|---|---|---|---|---|
| E2E-SUB-1 | `POST /v1/subscription/sync` с валидной тестовой транзакцией (`expiresDate` в будущем) | `200 {isSubscribed:true, expiresAt, plan}`; `subscriptions.status=active`; ledger `credit` на `SUBSCRIPTION_CREDITS_PER_PERIOD`; `/v1/wallet` показывает баланс 1000 | StoreKit-test | ADR-006 |
| E2E-SUB-2 | Повторный `sync` той же транзакции (тот же `transactionId`) | `200`; **grant не дублируется** (баланс остаётся 1000) — идемпотентность по `transactionId` | StoreKit-test | ADR-005/006 |
| E2E-SUB-3 | `sync` транзакции с `revocationDate` (revoked) | `200 {isSubscribed:false}`; `status=expired` | StoreKit-test | BR-5 |
| E2E-SUB-4 | `sync` с истёкшим `expiresDate` | `200 {isSubscribed:false}`; `status=expired` | StoreKit-test | BR-5 |
| E2E-SUB-5 | `sync` тестового JWS с невалидной подписью (неверный секрет) | `422` (technical error), подписка не меняется | StoreKit-test | §2.2 |
| E2E-SUB-6 | `subscription_change` audit-запись после каждого `sync` | audit содержит `subscription_change` с `transactionId`, `status`, без секретов | StoreKit-test | AC-7 |

### 4.2 Trial (BR-1, AC-1)
| ID | Сценарий | Ожидание | Зависимость |
|---|---|---|---|
| E2E-TRIAL-1 | Новый пользователь (нет подписки, `trial_used=false`), `POST /v1/chat/run` `mode=credits` | `200 status=assistant_message` (или `tool_call`) — первый trial разрешён; `users.trial_used` → true | **Claude** |
| E2E-TRIAL-2 | Тот же пользователь, второй `chat/run` `mode=credits` без подписки | `200 status=blocked, blockReason=trial_used` | — |

### 4.3 Credits debit + идемпотентность (AC-3, ADR-005/006)
| ID | Сценарий | Ожидание | Зависимость |
|---|---|---|---|
| E2E-CRED-1 | Активная подписка (после E2E-SUB-1), `chat/run` `mode=credits` до финального `assistant_message` | ровно **1 кредит** списан (баланс 1000→999); ledger `debit amount=1` по `messageStepId` | **Claude** |
| E2E-CRED-2 | Message-шаг с несколькими tool-раундами (run → tool_call → tool-result → … → assistant_message) | **ровно 1 debit** на весь шаг (промежуточные tool_call не списывают) | **Claude** |
| E2E-CRED-3 | Re-entry: повтор `/chat/tool-result` с тем же `toolCallId` (completed) | идемпотентно — не пересылается в Anthropic, повторного debit нет | **Claude** |
| E2E-CRED-4 | Баланс доведён до 0, `chat/run` `mode=credits` | `200 status=blocked, blockReason=credits_empty`; баланс не отрицателен | — |
| E2E-CRED-5 | `POST /v1/wallet/consume` дважды с одним `requestId`(=`messageStepId`) | одно списание (idempotency ledger) | — |
| E2E-CRED-6 | `wallet/consume` при balance < amount | отказ, баланс не отрицателен | — |

### 4.4 Blocked-кейсы по всем 8 blockReason (BR-5, ADR-004, AC-2)
Все `blocked` → **HTTP 200** с машиночитаемым `blockReason`.
| ID | blockReason | Состояние для воспроизведения | Зависимость |
|---|---|---|---|
| E2E-BLK-1 | `trial_used` | нет подписки, `trial_used=true`, `mode=credits` (см. E2E-TRIAL-2) | — |
| E2E-BLK-2 | `subscription_required` | нет подписки вообще, `mode=byok` | — |
| E2E-BLK-3 | `subscription_expired` | подписка `expired` (E2E-SUB-4), `mode=credits` или `byok` | StoreKit-test |
| E2E-BLK-4 | `credits_empty` | активная подписка, баланс 0, `mode=credits` (см. E2E-CRED-4) | StoreKit-test |
| E2E-BLK-5 | `byok_disabled` | активная подписка, `mode=byok`, BYOK toggle выключен | StoreKit-test |
| E2E-BLK-6 | `byok_invalid` | активная подписка, `mode=byok`, BYOK ключ `keyStatus=invalid` | StoreKit-test + **Claude** (валидация ключа) |
| E2E-BLK-7 | `rate_limited` | превысить per-user лимит `/v1/chat/run` (30 req/min дефолт) → **HTTP 429** (gateway rate-limit, стандартный error-формат с `code=rate_limited`, см. E2E-HTTP-5). `rate_limited` — **gateway-concern**, НЕ отражается в `/policy/effective.reasons[]` (policy engine не знает о rate-limit состоянии — см. BLK-7b ниже) | — |
| E2E-BLK-8 | `policy_denied` | общий fallback — достижим, если есть состояние не покрытое выше; иначе фиксируется как **недостижимый в текущей state-machine** с cross-ref на покрытие unit-тестами state-machine ([06-testing-strategy.md](06-testing-strategy.md)) | — |

> Если `policy_denied` структурно недостижим из публичного API при текущей state-machine (ADR-002),
> qa фиксирует это как обоснованное N/A в отчёте e2e (не как провал), со ссылкой на параметрический
> unit-тест state-machine, который покрывает ветку.

> **BLK-7b (исправление расхождения docs↔код).** `rate_limited` — это **gateway-concern**: он выражается
> исключительно как HTTP `429` (gateway rate-limit, E2E-HTTP-5/§4.8) и **НЕ** входит в
> `/policy/effective.reasons[]`. Policy Engine (`evaluate`, ADR-002) оперирует только состоянием
> subscription/trial/credits/byok и не знает rate-limit состояния, поэтому `reasons[]` строится из
> `evaluate()` без `rate_limited` — это корректное поведение, код policy не меняется.
> `rate_limited` остаётся значением **blockReason enum** (10 значений: `max_tokens` [ADR-025](adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md), `image_credits_empty` [ADR-058](adr/ADR-058-image-generation.md)) для HTTP-слоя и `/chat/run`,
> но из множества возможных значений `/policy/effective.reasons[]` исключён. qa проверяет E2E-BLK-7
> только по факту HTTP `429`; присутствия `rate_limited` в `/policy/effective` НЕ требуется и НЕ ожидается.

### 4.5 Tool-loop с реальным Claude (AC-4)
| ID | Сценарий | Ожидание | Зависимость |
|---|---|---|---|
> **[ADR-063](adr/ADR-063-remove-client-side-calendar-reminders-files-tools.md):** client-side инструменты (`files.*`/`calendar.*`/`reminders.*`) удалены — сценарии, завязанные на client-side `tool_call`→`/chat/tool-result` (E2E-TOOL-1/2/3/5/6), больше нельзя воспроизвести реальными инструментами. Многораундовый/мутирующий tool-loop с реальным Claude воспроизводится на **server-side** `site.*` (требуют проекта, исполняются на бэке); проверку самого client-side протокола (барьер, батч tool-result) — на **тестовом фейковом** client-side инструменте (фикстура, не поставляется). Зона qa (пересмотр сценариев).

| E2E-TOOL-1 | `chat/run` с промптом, провоцирующим минимум 2 раунда tool-use (server-side `site.list` → `site.read`, проект обязателен) | последовательность server-side раундов → `assistant_message`; `serverTools[]` строго по схемам | **Claude** |
| E2E-TOOL-2 | Мутирующий tool (`site.write_file` / `site.delete`) в loop | audit-запись на каждое мутирующее tool-действие (AC-7) | **Claude** |
| E2E-TOOL-3 | client-side `tool-result` c `error` вместо `result` (фейковый client-side инструмент) | backend передаёт Claude `is_error=true`, loop продолжается корректно | **Claude** + фейк-tool |
| E2E-TOOL-4 | `tool-result` с `toolCallId` чужой/несуществующей сессии | `404`/`403` | — |
| E2E-TOOL-5 | `tool-result` с `result`, нарушающим схему tool (фейковый client-side инструмент) | `422` | — |
| E2E-TOOL-6 | **Parallel tool use ([ADR-025](adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)):** несколько client-side tool_use в одном ходе (фейковый client-side инструмент, ×2) | ответ `tool_call` с `toolCalls[]` (≥2); continuation к Claude **только** после батч `tool-result` на все вызовы (барьер хода); ровно 1 debit на весь ход | **Claude** + фейк-tool |
| E2E-TOOL-7 | **max_tokens-обрезка ([ADR-025](adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)):** запрос с искусственно малым `ANTHROPIC_MAX_TOKENS` (через env), провоцирующий `stop_reason="max_tokens"` | `200 status=blocked`, `blockReason=max_tokens`, `usage`/`messageStepId`/`stepId` присутствуют, `toolCalls` отсутствуют, **кредит не списан** | **Claude** |

### 4.6 BYOK set/toggle/delete + routing (AC-5, BR-4)
| ID | Сценарий | Ожидание | Зависимость |
|---|---|---|---|
| E2E-BYOK-1 | `POST /v1/byok/set` с реальным валидным Anthropic-ключом | `200 {keyStatus:valid}`; в `byok_keys` — `encrypted_key`/`encrypted_dek`/`nonce`, plaintext нет | **Claude** |
| E2E-BYOK-2 | `set` с заведомо невалидным ключом | `200 {keyStatus:invalid}`, `byokEnabled=false` | **Claude** |
| E2E-BYOK-3 | `toggle enabled=true` при `keyStatus=invalid` | не включается: `{byokEnabled:false}` | — |
| E2E-BYOK-4 | `toggle enabled=true` при `keyStatus=valid` + активная подписка, затем `chat/run mode=byok` | генерация идёт через пользовательский ключ (routing); успешный ответ | **Claude** + StoreKit-test |
| E2E-BYOK-5 | `delete` | `{byokEnabled:false, keyStatus:missing}`; строка удалена | — |
| E2E-BYOK-6 | Логи всего прогона | не содержат plaintext BYOK-ключа, JWT, StoreKit/test-payload (redaction) | — |

### 4.7 Policy / effective консистентность (AC-6, BR-6)
| ID | Сценарий | Ожидание | Зависимость |
|---|---|---|---|
| E2E-POL-1 | Для набора состояний {none/active/expired} × {trial T/F} × {credits 0/>0} × {byok disabled/invalid/valid}: сравнить `GET /v1/policy/effective` с фактическим решением `chat/run` | `canGenerate{Credits,Byok}Mode` и `reasons[]` согласованы с `status`/`blockReason` из `chat/run` для каждого достижимого состояния | частично **Claude** (для allow-веток), StoreKit-test (для active) |

### 4.8 HTTP-семантика и служебные endpoint
| ID | Сценарий | Ожидание | Зависимость |
|---|---|---|---|
| E2E-HTTP-1 | Любой `/v1/*` без/с невалидным JWT | `401` | — |
| E2E-HTTP-2 | `userId` в теле ≠ `sub` JWT | `403` | — |
| E2E-HTTP-3a | Превышение **transport** body-лимита (общее тело > 512KB) | `413` (SizeLimitMiddleware, до парсинга) | — |
| E2E-HTTP-3b | Превышение **per-field schema**-лимита (`message` > 32KB при теле < 512KB) | `422` (Pydantic `max_length`, per-field schema violation — см. [05-security.md](05-security.md#size-лимиты-защита-payload)) | — |
| E2E-HTTP-4 | Невалидная схема (`extra`-поле, неизвестный `mode`/`toolName`) | `422` | — |
| E2E-HTTP-5 | Жёсткое превышение rate limit | `429` (стандартный error-формат с `code=rate_limited`) | — |
| E2E-HTTP-6 | Бизнес-blocked (любой из §4.4) | `200` с `blockReason` (НЕ 4xx) — подтверждает ADR-004 | разное |
| E2E-HTTP-7 | `GET /health`, `GET /ready` | `200`; `/ready` отражает доступность db+redis | — |
| E2E-DOCS-1 | `GET /docs`, `/redoc`, `/openapi.json` при `DOCS_ENABLED=true` | `200`; OpenAPI на русском, JWT Bearer scheme, теги по модулям ([08-api-documentation.md](08-api-documentation.md)) | — |
| E2E-DOCS-2 | Те же пути при `DOCS_ENABLED=false` (отдельный прогон/перезапуск) | `404` | — |

### 4.9 Ленивый провижининг users (ADR-007, регресс BUG-1)
Новый `sub`, для которого строка `users` **никогда не создавалась** (никаких предварительных фикстур/seed). Источник истины идентичности — JWT `sub`, регистрации нет ([ADR-007](adr/ADR-007-lazy-user-provisioning.md), [05-security.md](05-security.md#модель-идентичности-и-провижининг-пользователей)).
| ID | Сценарий | Ожидание | Зависимость |
|---|---|---|---|
| E2E-PROV-1 | Первый аутентифицированный **write**-запрос нового `sub` (`POST /v1/subscription/sync` test-mode **или** `POST /v1/chat/run`) | **НЕ 500**: строка `users` создаётся лениво до FK-вставки; flow проходит (`200`). В `users` появляется ровно одна строка с `id = sub` | StoreKit-test (для sync) / **Claude** (для chat/run) |
| E2E-PROV-2 | Второй запрос того же `sub` | `200`; **дубликат `users` не создаётся** (идемпотентный `ON CONFLICT DO NOTHING`); существующие `trial_used`/`created_at` не перезаписаны | — |
| E2E-PROV-3 | Параллельные первые запросы одного нового `sub` (конкурентность) | оба `200`, ровно одна строка `users`, без `ForeignKeyViolationError`/`500` (race-free upsert) | — |

---

## 5. Definition of Done для e2e-прогона
> Статус: **выполнено** (e2e-прогон от 2026-05-25, qa: 358/358 unit + полный live §4 зелёные против реального Claude, `production_ready=true`). Evidence — в каждом пункте.

- [x] Ленивый провижининг users (ADR-007) реализован; §4.9 (E2E-PROV-1..3) проходит — write-path нового `sub` не падает с 500. _Evidence: write нового `sub` (`/subscription/sync`, `/chat/run`) проходит без 500; ровно одна строка `users`, `ON CONFLICT DO NOTHING` — без дубликатов и без `ForeignKeyViolationError` при конкуренции._
- [x] TD-008 (migrations/env.py) исправлен; `migrate`-job в compose проходит. _Evidence: миграция `0002` (`provider_tool_use_id`, ADR-008) применена; `GET /ready` → `db=ok`._
- [x] STOREKIT_TEST_MODE реализован по §2; при `false` prod-поведение не изменилось (реальная JWS-верификация, fail-closed). _Evidence: при `false` — fail-closed сохранён; E2E-SUB-5 (bad-sig тестовый JWS) → `422`._
- [x] Контейнеры подняты, `api` healthy через `/ready`. _Evidence: bring-up `docker compose -f docker-compose.yml -f docker-compose.e2e.yml up -d` (см. §3.3); `api` → `service_healthy`._
- [x] Все сценарии §4 прогнаны против живого сервиса; `Claude`-сценарии — против реального Anthropic; StoreKit — через test-mode. _Evidence: весь §4 зелёный; tool-loop/BYOK-валидация — против реального Anthropic; StoreKit — через HS256 test-mode._
- [x] Все 8 `blockReason` покрыты (или обоснованное N/A для `policy_denied` с cross-ref на unit-покрытие). _Evidence: 7 blockReason покрыты e2e; `policy_denied` — обоснованный N/A (структурно недостижим из публичного API, cross-ref на параметрический unit-тест state-machine, [06-testing-strategy.md](06-testing-strategy.md))._
- [x] Логи прогона проверены на отсутствие секретов (BYOK/JWT/StoreKit/test-payload). _Evidence: leaks=none — plaintext BYOK-ключа, JWT, StoreKit/test-payload в логах нет (redaction-allowlist, [05-security.md](05-security.md))._
- [x] Итог: сервис работает на 100% без багов; расхождения docs↔поведение отсутствуют. _Evidence: 0 fail по §4; все найденные дефекты были в harness/тестах (`blame:test`), не в сервисе; расхождений docs↔поведение нет._

## 6. Scope
**Backend (перед e2e):**
1. Реализовать ленивый провижининг users по [ADR-007](adr/ADR-007-lazy-user-provisioning.md) (CRITICAL, BUG-1): в `get_current_user` (API Gateway) после JWT-верификации и до downstream — идемпотентный `INSERT INTO users (id) VALUES (:sub) ON CONFLICT (id) DO NOTHING` в текущей сессии БД, до любой FK-зависимой операции. Покрывает все write-эндпоинты единообразно. Добавить регресс-тест без seed users.
2. Исправить `migrations/env.py` ([TD-008](100-known-tech-debt.md)).
3. Реализовать `STOREKIT_TEST_MODE` + `STOREKIT_TEST_SECRET` по §2 ([TD-007](100-known-tech-debt.md)); добавить настройки в `config.py`, ветку в `StoreKitVerifier.verify`, startup-WARNING.
4. Никаких изменений в Anthropic-интеграции (только конфигурация).

**QA:** прогнать §4 против поднятых контейнеров, заполнить DoD §5, отчитаться по каждому ID (pass/fail/N-A с обоснованием).
