# Module: BYOK

- Статус: Реализован (⏳ доработка ADR-044 — мульти-провайдерный BYOK)
- Ответственность: envelope-шифрование пользовательского ключа **любого поддерживаемого провайдера** (Anthropic `sk-ant-…` ИЛИ OpenAI `sk-…`/`sk-proj-…`), toggle, delete, валидация **через провайдера, определённого по ключу** ([ADR-044](../../adr/ADR-044-multi-provider-byok.md)), выдача plaintext ключа Orchestrator in-memory.

## Документы
- [00-overview.md](00-overview.md) · [01-context.md](01-context.md) · [02-api-contracts.md](02-api-contracts.md) · [03-architecture.md](03-architecture.md) · [04-data-model.md](04-data-model.md) · [06-rbac.md](06-rbac.md) · [07-implementation-phases.md](07-implementation-phases.md) · [09-testing.md](09-testing.md)

## DoD
Ключ шифруется at-rest (envelope, AES-256-GCM + KMS); никогда не логируется и не возвращается клиенту; toggle/delete работают; key_status корректен. `keyStatus` — 6 значений (`valid`/`invalid`/`missing`/`validating`/`offline`/`expired`, [ADR-016](../../adr/ADR-016-extended-byok-statuses.md)); при `keyStatus=valid` ответ содержит `activeModel`, иначе `null`.

## Changelog
- 2026-05-21: bootstrap (architect).
- 2026-05-21: реализован (backend), тесты зелёные, ревью пройдено. Код: `src/app/byok/service.py`, `src/app/byok/kms.py` (`KmsClient` + `LocalKmsClient`, дефолт для local/CI; облачный провайдер — Q-002-1).
- 2026-06-02 (Спринт 1, backend, [ADR-016](../../adr/ADR-016-extended-byok-statuses.md)): `keyStatus` расширен до 6 значений (`+validating`/`offline`/`expired`); добавлено поле `activeModel` (модель при `valid`, иначе `null`). Миграция `0004` расширяет enum `byok_key_status` (`ALTER TYPE ... ADD VALUE`). Старые клиенты трактуют новые статусы как «не valid». Тесты зелёные (offline-сьют 681/681).
- 2026-06-25 (architect, [ADR-044](../../adr/ADR-044-multi-provider-byok.md), спроектировано — ожидает реализации backend): **мульти-провайдерный BYOK**. Провайдер ключа определяется детектором префиксов `detect_byok_provider` (`src/app/byok/provider_detect.py`: `sk-ant-`→anthropic РАНЬШЕ `sk-`/`sk-proj-`→openai; иначе→`None`); валидация и генерация byok идут через `llm_client_for(provider)` независимо от `LLM_PROVIDER`. Новая колонка `byok_keys.provider TEXT NULL` (миграция `0013`, expand-only, без backfill). `activeModel` и генерационная модель — BYOK-дефолт **определённого** провайдера. Ревизует [ADR-033 §7](../../adr/ADR-033-llm-provider-abstraction.md)/[ADR-016](../../adr/ADR-016-extended-byok-statuses.md). Контракт `/v1/byok/*` не меняется.
