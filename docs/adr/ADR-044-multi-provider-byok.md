# ADR-044 — Мульти-провайдерный BYOK (ключ любого провайдера независимо от LLM_PROVIDER инстанса)

- **Статус:** Accepted
- **Дата:** 2026-06-25
- **Тип:** implementation-ADR (расширяет [ADR-033](ADR-033-llm-provider-abstraction.md) §7/§8 и [ADR-016](ADR-016-extended-byok-statuses.md); затрагивает [ADR-003](ADR-003-byok-envelope-encryption.md), [ADR-034](ADR-034-user-model-selection.md))
- **Связано:** [ADR-033](ADR-033-llm-provider-abstraction.md) (провайдер-абстракция `LLMClient`, factory, один сервисный провайдер на инстанс), [ADR-016](ADR-016-extended-byok-statuses.md) (BYOK-статусы + `activeModel`), [ADR-003](ADR-003-byok-envelope-encryption.md) (envelope encryption, `get_plaintext_key`), [ADR-034](ADR-034-user-model-selection.md) (выбор модели, `chat_sessions.model`, `allowed_models()`), [ADR-006](ADR-006-credit-billing-and-subscription-grant.md) (биллинг неизменен)

> **Ревизия [ADR-033 §7](ADR-033-llm-provider-abstraction.md) и [ADR-016](ADR-016-extended-byok-statuses.md) в части BYOK-провайдера.** ADR-033 §7 зафиксировал: «BYOK на инстансе валидирует ключ **своего** провайдера». Это правило **снимается** настоящим ADR для BYOK: BYOK-ключ обрабатывается провайдером, **определённым по самому ключу**, независимо от `LLM_PROVIDER` инстанса. Сервисный (credits-режим) провайдер инстанса по-прежнему один (ADR-033 инвариант «один сервисный провайдер на инстанс» в силе). Тело ADR-033 не переписывается (immutability) — актуальное BYOK-поведение см. здесь.

## Контекст

Все инстансы переводятся на `LLM_PROVIDER=openai` (сервисный credits-режим = OpenAI/`gpt-4o`). При этом часть клиентов хочет приносить **Anthropic**-ключи через BYOK (`sk-ant-…`). Сейчас ([ADR-033 §7](ADR-033-llm-provider-abstraction.md)) BYOK жёстко завязан на провайдер инстанса:

- `byok/service.py::validate_key(api_key)` валидирует ключ через нейтральный `LLMClient` **активного** провайдера (`get_llm_client()` по `LLM_PROVIDER`).
- `byok/service.py::_active_model_for` выбирает дефолт-модель по `settings.llm_provider`.
- `chat/orchestrator.py` (mode=byok): `_resolve_api_key` (расшифровка `get_plaintext_key`) → генерация **активным** клиентом инстанса с `with_options(api_key=...)`.

Следствие: Anthropic-ключ, введённый на OpenAI-инстансе, валидируется через OpenAI (`models.list` под `sk-ant-…`) → 401 → `invalid`; даже при «valid» генерация шла бы OpenAI-клиентом под чужим ключом. **Anthropic BYOK на OpenAI-инстансе сломан.**

Факты кода (подтверждено), на которые опирается решение:
- `AnthropicClient` и `OpenAIClient` — обе реализации нейтрального `LLMClient` ([ADR-033 §1](ADR-033-llm-provider-abstraction.md)); обе SDK (`anthropic`, `openai`) в стеке всегда; конструкторы обоих читают только конфиг и **не зависят** от `LLM_PROVIDER` — каждый клиент конструируется на любом инстансе.
- Обе реализуют `validate_key(api_key)` и `create_message(..., api_key=...)` с per-call override через `with_options(api_key=...)`. `validate_key`: 401 → `invalid`, network/non-401 → `offline`, ok → `valid` (симметрично, [ADR-016](ADR-016-extended-byok-statuses.md)).
- `get_llm_client()` ([ADR-033 §8](ADR-033-llm-provider-abstraction.md)) возвращает клиент **активного** провайдера; OpenAI-синглтон уже живёт в `llm_client.py`, Anthropic-синглтон — в `anthropic_client.py`.
- `byok_keys` хранит `encrypted_key`/`encrypted_dek`/`nonce`/`key_status`/`enabled` ([ADR-003](ADR-003-byok-envelope-encryption.md)); plaintext-ключ и DEK не хранятся.
- BYOK-дефолт-модель per-provider в конфиге: `BYOK_DEFAULT_MODEL` (anthropic) / `OPENAI_BYOK_DEFAULT_MODEL` (openai) ([ADR-033 §7](ADR-033-llm-provider-abstraction.md)).

## Решение

BYOK становится **мульти-провайдерным**: провайдер ключа определяется **по самому ключу** (детектор префиксов), а не по `LLM_PROVIDER`. Валидация и генерация в режиме byok идут через клиент **определённого по ключу** провайдера, построенный фабрикой независимо от провайдера инстанса.

### 1. Детектор провайдера по префиксу ключа

Чистая функция `detect_byok_provider(api_key: str) -> str | None` (новый модуль `src/app/byok/provider_detect.py`, single source of truth детекции). Правила (порядок проверки **строго** сверху вниз — `sk-ant-` РАНЬШЕ `sk-`):

| Приоритет | Условие (после `strip`) | Результат |
|---|---|---|
| 1 | начинается с `sk-ant-` | `"anthropic"` |
| 2 | начинается с `sk-proj-` | `"openai"` |
| 3 | начинается с `sk-` | `"openai"` |
| 4 | иначе | `None` (не определён) |

- **Приоритет 1 раньше 2/3 обязателен:** `sk-ant-…` также начинается с `sk-`, поэтому Anthropic-проверка должна стоять до OpenAI-проверки, иначе Anthropic-ключ ошибочно классифицируется как OpenAI.
- `sk-proj-` (OpenAI project-scoped) — частный случай OpenAI; явная ветка для читаемости (приоритеты 2 и 3 дают один результат `"openai"`, но 2 документирует намерение).
- Сравнение — по **известным префиксам**, без регэкспов на тело ключа; функция **не логирует** ключ и не raise (чистая, возвращает значение). Ключ нормализуется только `strip()` (ведущие/хвостовые пробелы), тело не трансформируется.
- `None` → провайдер не определён. Поведение по `None`: на **валидации** (`set`) → терминальный статус `invalid` (ключ неизвестного формата, без вызова какого-либо провайдера); на **использовании** (генерация) → defensive-block `byok_invalid` (этот путь не должен достигаться: невалидный/неизвестный ключ не доходит до `valid`/`enabled`).
- Допустимые значения возврата ограничены каноническим множеством провайдеров `{"anthropic", "openai"}` (то же, что `LLM_PROVIDER`). При появлении нового провайдера расширяется и детектор, и фабрика (§2) — единый набор.

### 2. Фабрика клиента по провайдеру (независимо от LLM_PROVIDER)

Вводится `llm_client_for(provider: str) -> LLMClient` (в `src/app/chat/llm_client.py`, рядом с `get_llm_client()`):

- `provider == "anthropic"` → Anthropic-клиент (тот же process-wide синглтон, что `get_anthropic_client()` — чтобы conftest-патч `_anthropic_singleton` продолжал перекрывать и этот путь);
- `provider == "openai"` → OpenAI-клиент (тот же `_openai_singleton`, что использует `get_llm_client()` на openai-пути);
- иной/неизвестный provider → `ValueError` (внутренняя ошибка вызывающего; детектор §1 гарантирует только `{anthropic, openai}` перед вызовом).

Выбор `llm_client_for(provider)` (а не `get_llm_client(provider=...)` с дефолтом из env):

- **Семантическая ясность:** имя явно требует провайдер — нельзя случайно вызвать «активный».
- `get_llm_client()` остаётся **без сигнатурного изменения** (читает `LLM_PROVIDER`) → существующий credits-путь и все его вызовы не трогаются. `get_llm_client()` рефакторится так, чтобы делегировать в `llm_client_for(active_provider)` (единый источник синглтонов), но публичная сигнатура и поведение не меняются.

Оба клиента кэшируются как process-wide синглтоны (как сейчас), независимо от `LLM_PROVIDER`. На OpenAI-инстансе при первом Anthropic-BYOK-вызове лениво создаётся Anthropic-синглтон (его конструктор читает `ANTHROPIC_API_KEY`/`ANTHROPIC_MODEL` из конфига — для BYOK сервисный ключ не используется, см. §5). Симметрично на Anthropic-инстансе для OpenAI-BYOK.

### 3. Валидация (byok/service `set_key`)

`BYOKService.set_key(user_id, api_key)`:

1. `provider = detect_byok_provider(api_key)`.
   - `provider is None` → `key_status = "invalid"` **без** какого-либо сетевого вызова (неизвестный формат — нечего и некому валидировать). Сохранить зашифрованно со статусом `invalid` (как любой невалидный ключ), `enabled=False`, `provider=NULL` (формат не распознан). Вернуть `BYOKResult(byok_enabled=False, key_status="invalid", active_model=None)`.
2. Иначе `client = llm_client_for(provider)` → `validation = await client.validate_key(api_key)` → маппинг как сейчас (`valid`/`invalid`/`offline`, [ADR-016](ADR-016-extended-byok-statuses.md)).
3. Envelope-encryption (`set`) — **без изменений** ([ADR-003](ADR-003-byok-envelope-encryption.md)): DEK→AES-GCM→KMS-wrap. Дополнительно сохранить `provider` в новой колонке (§4) — для `valid`/`invalid`/`offline` это **определённый детектором** провайдер (не зависит от исхода валидации).
4. `activeModel` — дефолт-модель **определённого по ключу** провайдера (§6 `_active_model_for`): `anthropic → BYOK_DEFAULT_MODEL`; `openai → OPENAI_BYOK_DEFAULT_MODEL`. Заполняется только при `key_status=valid` ([ADR-016](ADR-016-extended-byok-statuses.md), без изменений).

`toggle`/`delete`/`get_status` — провайдер не нужен для семантики статуса; `_active_model_for` (§6) определяет дефолт-модель по **сохранённому `provider`** строки (а не по `LLM_PROVIDER`).

Статусы и переходы [ADR-016](ADR-016-extended-byok-statuses.md) **сохраняются дословно**: `missing → validating → (valid|invalid|offline)`; runtime-401 на ранее `valid` → `expired`; `toggle enabled=true` только при `valid`.

### 4. Хранение провайдера: колонка `byok_keys.provider` (миграция 0013)

**Решение: хранить provider в `byok_keys` (выбран вариант «колонка», не детект-at-use).**

```sql
ALTER TABLE byok_keys ADD COLUMN provider TEXT NULL;
```

- `NULL` = провайдер неизвестен/не записан (легаси-строки до миграции; либо `set` с нераспознанным форматом — §3.1). Не enum, а `TEXT` (как `auth_identities.provider`) — расширяемость без `ALTER TYPE`; допустимые значения `{anthropic, openai}` валидируются приложением (детектор §1), не БД-ограничением (симметрия с подходом к provider-строкам в проекте).
- **Backfill не делается** (expand-only, как `0009`/`0010`): легаси-строки получают `provider=NULL`. Для `NULL`-строки `_active_model_for` (§6) и генерация (§5) **детектят провайдер на лету по расшифрованному ключу** (fallback) — корректность сохраняется без миграции данных; запись свежего `provider` происходит при следующем `set`.
- Колонка пишется при каждом `set_key` (включая повторный/реset) — определённым детектором провайдером.

**Почему колонка, а не чистый детект-at-use:**

- `get_status`/`set`/`toggle` отдают `activeModel` (дефолт-модель провайдера) **без расшифровки ключа** — расшифровка (`get_plaintext_key`) делает KMS-Decrypt + AES-GCM и должна вызываться **только** на генерации (минимизация работы с plaintext, [ADR-003](ADR-003-byok-envelope-encryption.md) §«plaintext только на время одного вызова»). Без колонки пришлось бы расшифровывать ключ на каждый `GET /v1/byok` ради `activeModel` — недопустимое расширение поверхности plaintext.
- Предсказуемость: `activeModel`/статусная семантика не зависят от исхода детекции на каждом чтении.
- Fallback-детект (для `NULL`-строк) сохраняет корректность без принудительного backfill.

### 5. Генерация (orchestrator byok-путь)

Изменяется только byok-ветка `_generate_loop` (через `_resolve_api_key` и выбор клиента/модели). Credits-путь не трогается.

1. `_resolve_api_key` (mode=byok) — как сейчас: `get_plaintext_key(user_id)` (in-memory, не логируется). Дополнительно вернуть/определить **провайдер ключа**: предпочтительно из строки `byok_keys.provider` (одно чтение, без расшифровки ради провайдера); при `provider IS NULL` (легаси) — `detect_byok_provider(plaintext_key)` как fallback. Провайдер `None` после fallback → defensive-block `byok_invalid` (не должно достигаться при `valid`-ключе).
2. **Клиент генерации** = `llm_client_for(byok_provider)` (НЕ `self._deps.llm` активного провайдера). То есть в byok-режиме orchestrator использует клиент, определённый по ключу, а не инстансный.
3. **Модель генерации** = BYOK-дефолт **определённого по ключу** провайдера (`BYOK_DEFAULT_MODEL` / `OPENAI_BYOK_DEFAULT_MODEL`), **НЕ** сессионная `chat_sessions.model`, если та принадлежит другому провайдеру. Точное правило выбора модели для byok-генерации:
   - если сессионная `sess.model` задана **и** входит в allowlist **byok-провайдера** (проверка против provider-aware allowlist того провайдера, `allowed_models_for(byok_provider)`, по аналогии с `allowed_models()`, но для целевого провайдера) → использовать `sess.model`;
   - иначе orchestrator **ЯВНО** подставляет BYOK-дефолт провайдера ключа: `byok_default_model_for(byok_provider)` (`BYOK_DEFAULT_MODEL` для anthropic / `OPENAI_BYOK_DEFAULT_MODEL` для openai) и передаёт его в `create_message(model=…)`. **Важно:** `model=None` клиенту в byok-ветке **НЕ** передаётся — при `model=None` клиент берёт свой **сервисный** дефолт (`settings.<provider>_model`, тот же что для credits), а НЕ BYOK-дефолт; поэтому BYOK-дефолт подставляется явно. Сессионная модель чужого провайдера (`claude-*` на OpenAI-ключе и наоборот) **никогда** не передаётся клиенту другого провайдера → нет `create_message(model=чужая)`-ошибки.
   - Это симметрично credits-stale-model-фоллбэку (§«Связанное»): если зафиксированная за сессией модель несовместима с фактически используемым провайдером, не падаем — фолбэк на дефолт.
4. Per-call ключ передаётся в `create_message(api_key=plaintext_key, model=<выбранная или None>)` выбранного клиента; после вызова `api_key` обнуляется (как сейчас).
5. Runtime-401 (`AnthropicAuthError`/`OpenAIAuthError`) на byok → `mark_expired` ([ADR-016](ADR-016-extended-byok-statuses.md)) — без изменений (оба типа уже ловятся в `_generate_loop`).
6. Биллинг byok-генерации **текста** — **бесплатно**, без изменений ([ADR-006](ADR-006-credit-billing-and-subscription-grant.md), `_billing_plan`: `mode is byok → no debit, no trial`). *(Уточнение [ADR-058 §4](ADR-058-image-generation.md): это про текст через ключ пользователя; `_billing_plan` не меняется. Генерация **изображений** — отдельный дебет `IMAGE_CREDITS_COST` **вне** `_billing_plan`, списывается и при `byok` — изображение идёт через наш серверный `OPENAI_API_KEY`.)*

**tool-loop, server-side tools, attachments, нормализация payload, барьер хода, `seq`-порядок** — провайдер-агностичны ([ADR-033 §3/§4/§5/§6](ADR-033-llm-provider-abstraction.md)) и работают для byok-провайдера так же, как для сервисного. Один диалог byok ведётся одним провайдером (определённым по ключу пользователя) — кросс-провайдерного реплея в одной сессии не возникает (инвариант [ADR-033](ADR-033-llm-provider-abstraction.md)/[TD-024](../100-known-tech-debt.md) сохраняется: byok-сессия одно-провайдерна по построению, провайдер ключа стабилен в рамках пользователя).

### 6. `_active_model_for` per-stored-provider

`byok/service.py::_active_model_for(key_status, provider)` (сигнатура расширяется аргументом `provider`):

- `key_status != "valid"` → `None` (без изменений).
- `provider == "openai"` → `settings.openai_byok_default_model`.
- `provider == "anthropic"` → `settings.byok_default_model`.
- `provider is None` (легаси-строка) → fallback: расшифровать недоступно дёшево на чтении → допустимо вернуть дефолт **активного** инстанса как ранее (деградация только для до-миграционных строк до их следующего `set`; на практике редко, не влияет на безопасность). Документируется как известное ограничение легаси-строк, закрывается естественно при следующем `set`.

### 7. Credits-режим не меняется

Сервисный (credits) провайдер инстанса остаётся ОДИН (`LLM_PROVIDER`, [ADR-033](ADR-033-llm-provider-abstraction.md)). Мульти-провайдерность вводится **только для byok**. `get_llm_client()` (активный клиент) по-прежнему используется для всей credits/trial-генерации; `self._deps.llm` инжектится как раньше. Никаких изменений в policy, биллинге, провижининге.

### 8. Безопасность

- **Ключ не логируется** нигде: детектор (§1) чистый и не логирует; `validate_key`/`create_message` не логируют ключ ([ADR-003](ADR-003-byok-envelope-encryption.md), [05-security.md](../05-security.md)). `OPENAI_API_KEY`/`ANTHROPIC_API_KEY`/byok-ключ — под redaction (`*key*`).
- Детект — **только по известным префиксам**; неизвестный формат → `invalid` без сетевого вызова (не зондируем сторонние провайдеры произвольным вводом).
- Plaintext-ключ расшифровывается **только** на генерации (§5), как раньше; колонка `provider` (§4) исключает расшифровку ради `activeModel` на чтениях.
- **Обратная совместимость:** Anthropic-инстанс + Anthropic-ключ (`sk-ant-…`) → детект `anthropic` → `llm_client_for("anthropic")` = тот же клиент, что и `get_llm_client()` на anthropic-инстансе → поведение идентично доавтор-ADR-044 (та же валидация, та же генерация, та же модель). OpenAI-инстанс + OpenAI-ключ — симметрично. Изменение наблюдаемо только для «ключ одного провайдера на инстансе другого».
- `provider`-колонка не секрет (имя провайдера), безопасна; не отдаётся в публичных ответах BYOK (контракт `{byokEnabled, keyStatus, activeModel}` не расширяется — §Последствия).

## Альтернативы

- **Оставить ADR-033 §7 (byok = провайдер инстанса).** Отклонено: ломает заявленную цель (Anthropic BYOK на OpenAI-инстансе невозможен). Это и есть проблема, ради которой вводится ADR.
- **Детект-at-use без колонки `provider`.** Отклонено как основной путь: вынуждает расшифровывать ключ на каждый `GET /v1/byok` ради `activeModel` (расширяет поверхность plaintext, противоречит [ADR-003](ADR-003-byok-envelope-encryption.md)) либо терять `activeModel` на чтениях. Детект-at-use оставлен только как **fallback** для легаси `NULL`-строк (§4/§6).
- **Хранить провайдер в `key_status`/доп. флаге вместо отдельной колонки.** Отклонено: смешивает ортогональные оси (статус валидности vs провайдер); enum `byok_key_status` пришлось бы дублировать per-provider. Чистая `TEXT provider` проще и расширяема.
- **`get_llm_client(provider=...)` с дефолтом из env вместо `llm_client_for`.** Отклонено: перегрузка сигнатуры активного-клиента риск случайного «активного» вызова; явная `llm_client_for(provider)` безопаснее и читаемее. `get_llm_client()` сохраняет точную текущую сигнатуру.
- **Разрешить выбор byok-модели из allowlist чужого провайдера.** Не применимо: allowlist per-provider ([ADR-034](ADR-034-user-model-selection.md)); модель чужого провайдера несовместима. Byok-генерация использует BYOK-дефолт провайдера ключа или сессионную модель только если она того же провайдера (§5.3).

## Последствия

- **Положительные:** клиент приносит ключ ЛЮБОГО из поддерживаемых провайдеров на ЛЮБОЙ инстанс; Anthropic BYOK работает на OpenAI-инстансах (и наоборот); credits-режим и существующие инстансы не затронуты (полная обратная совместимость для «ключ = провайдер инстанса»); переиспользуется вся провайдер-абстракция ([ADR-033](ADR-033-llm-provider-abstraction.md)).
- **Цена:** на инстансе в памяти могут жить оба клиентских синглтона (anthropic + openai) — оба SDK уже в стеке, оверхед мал; миграция `0013` (+1 nullable-колонка, expand-only); byok-ветка orchestrator выбирает клиент/модель по провайдеру ключа (а не инстанса).
- **Контракт BYOK не меняется:** публичные ответы `/v1/byok/*` остаются `{byokEnabled, keyStatus, activeModel}` ([ADR-016](ADR-016-extended-byok-statuses.md)); `provider` — внутреннее поле строки, наружу не отдаётся (iOS определяет провайдера по формату ключа/`activeModel` самостоятельно; расширение ответа полем `provider` — при необходимости отдельным аддитивным изменением, не требуется этим ADR).
- **Tech debt:** легаси `byok_keys`-строки с `provider=NULL` используют fallback-детект на генерации и дефолт активного инстанса для `activeModel` до следующего `set` ([TD-029](../100-known-tech-debt.md)). Кросс-провайдерный реплей в одной сессии по-прежнему вне scope ([TD-024](../100-known-tech-debt.md)) — не возникает (byok-сессия одно-провайдерна).
- **Безопасность:** ключ не логируется; детект только по префиксам; plaintext только на генерации; обратная совместимость полная.

## Связанное — перевод инстансов на `LLM_PROVIDER=openai`: stale-model фолбэк (часть этого ADR, для backend)

При смене `LLM_PROVIDER` инстанса существующие `chat_sessions.model` могли быть зафиксированы за моделью **другого** провайдера ([ADR-034 §3](ADR-034-user-model-selection.md): модель session-fixed, на resume берётся из сессии и поле запроса игнорируется). Например, чат создан на Anthropic-инстансе с `model="claude-sonnet-4-5"`, инстанс переведён на `LLM_PROVIDER=openai`; при resume в **credits-режиме** orchestrator вызовет активный OpenAI-клиент с `model="claude-sonnet-4-5"` → `chat.completions.create(model=claude-*)` → ошибка провайдера (`unknown model`) → 502. Пользователь не может продолжить старый чат.

**Политика-фикс (нормативно для backend), credits-режим:**

- На **resume** в credits-режиме (а равно при любой генерации credits) перед передачей `model` клиенту проверять членство `sess.model` в `allowed_models()` **активного** провайдера.
- Если `sess.model` **не** в allowlist активного провайдера → передать в `create_message(model=None)` (клиент возьмёт свой провайдерный дефолт), **не падать**. То есть: stale/чужая зафиксированная модель → graceful-фолбэк на дефолт активного провайдера.
- Реализация: точка — `ChatOrchestrator.run`/`tool_result` там, где сейчас `model=sess.model or None` передаётся в `_generate_loop`. Заменить на helper «`sess.model` если он в `allowed_models()` активного провайдера, иначе `None`». Применяется к **обоим** входам (`run` resume и `tool_result` continuation).
- `chat_sessions.model` в БД **не переписывается** (остаётся исторической отметкой выбора; expand-only-принцип, как [ADR-034](ADR-034-user-model-selection.md) не переписывает model на resume); меняется только то, что передаётся клиенту на вызове.
- Создание **новой** сессии не затронуто: `model`-allowlist-валидация на create ([ADR-034 §3](ADR-034-user-model-selection.md)) уже проверяет против активного провайдера → чужая модель на create → `422` как раньше. Фолбэк нужен именно для **resume** ранее зафиксированных сессий.
- В **byok-режиме** аналог уже описан в §5.3 (модель проверяется против allowlist провайдера **ключа**, иначе `None`). Разница: credits проверяет против активного провайдера, byok — против провайдера ключа.

Этот фолбэк делает перевод `LLM_PROVIDER` инстанса безопасным для существующих чатов (старые чаты продолжаются на новом дефолте, а не падают).
