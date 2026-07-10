# ADR-016 — Расширенные BYOK-статусы + активная модель в ответе

- Статус: Accepted (ревизия 2026-06-25 — мульти-провайдерный BYOK, [ADR-044](ADR-044-multi-provider-byok.md))
- Дата: 2026-06-02
- Связан с: ADR-003 (BYOK envelope encryption), [ADR-033](ADR-033-llm-provider-abstraction.md) (провайдер-абстракция), [ADR-044](ADR-044-multi-provider-byok.md) (мульти-провайдерный BYOK), модуль `byok`.

> **Ревизия 2026-06-25 ([ADR-044](ADR-044-multi-provider-byok.md)).** Статусы и переходы ниже **сохраняются дословно**. Меняется источник провайдера для валидации и для `activeModel`: ранее — провайдер инстанса (`LLM_PROVIDER`); теперь — провайдер, **определённый по самому ключу** (детектор префиксов `sk-ant-`→anthropic / `sk-`/`sk-proj-`→openai). `activeModel` = BYOK-дефолт определённого провайдера (`BYOK_DEFAULT_MODEL` / `OPENAI_BYOK_DEFAULT_MODEL`), хранится через новую колонку `byok_keys.provider` (миграция `0013`). Контракт ответа `{byokEnabled, keyStatus, activeModel}` не меняется. Детали — [ADR-044](ADR-044-multi-provider-byok.md).

## Context
Дизайн различает больше состояний BYOK-ключа, чем текущие `valid | invalid | missing`:
- **Not set** — ключ не задан → `missing`.
- **Checking (validating)** — идёт валидация ключа.
- **Connected + Active + модель** — ключ валиден, показывается активная модель (`claude-sonnet-4-6`).
- **Invalid (401)** — ключ отклонён Anthropic (401 Unauthorized).
- **Offline (network error)** — валидацию не удалось выполнить из-за сети (НЕ 401).
- **Expired (revoked)** — ранее валидный ключ перестал работать (отозван/истёк).

Текущий enum схлопывает Checking/Offline/Expired в `invalid`/`missing`, и не возвращает активную модель.

## Decision
1. **Расширить enum `byok_key_status`** добавлением значений (expand-only, обратная совместимость):
   `validating`, `offline`, `expired` (в дополнение к `valid`, `invalid`, `missing`). Старые значения сохраняют семантику:
   - `missing` ← Not set
   - `validating` ← Checking
   - `valid` ← Connected + Active
   - `invalid` ← Invalid (401)
   - `offline` ← сетевая ошибка валидации
   - `expired` ← был valid, стал недействителен (отзыв/истечение, обнаружено при использовании в `/chat/run`)

2. **Возврат активной модели.** Добавить в BYOK-ответ опциональное поле `activeModel` (string, напр. `claude-sonnet-4-6`) — заполняется при `keyStatus=valid`. Источник: конфиг дефолтной модели для BYOK (`BYOK_DEFAULT_MODEL`) или модель, подтверждённая при валидации. При `keyStatus != valid` — `null`.

3. **Обратная совместимость контракта.** Базовая форма ответа `{ byokEnabled, keyStatus }` сохраняется; `activeModel` добавляется как новое опциональное поле. Клиенты, знающие только `valid|invalid|missing`, продолжают работать: новые статусы клиент трактует как «не valid» (UI degrade graceful). Это **не** breaking change.

### Переходы статусов (нормативно)
- `set`: `missing → validating → (valid | invalid | offline)`. `validating` — транзиентное состояние во время лёгкого вызова Anthropic; при сетевой ошибке → `offline` (а не `invalid`), при 401 → `invalid`, при успехе → `valid`.
- Использование в `/chat/run` (mode=byok): если ранее `valid`, но Anthropic вернул 401 → перевести в `expired` (ключ отозван). Сетевая ошибка при использовании не меняет статус (транзиентно).
- `toggle enabled=true` допускается только при `keyStatus=valid` (как в ADR-003/byok-контракте; расширенные статусы `validating/offline/expired` не позволяют включить).

## Consequences
- Миграция `0004` добавляет 3 значения в enum (PostgreSQL `ALTER TYPE ... ADD VALUE`).
- `byok` module и `/policy/effective` уточняют: `byokEnabled` = `enabled && key_status==valid` (без изменений — новые статусы не равны valid, поэтому policy не ломается). `reasons[]` `byok_invalid` покрывает все не-valid не-missing состояния для целей policy.
- BYOK-ответы дополняются `activeModel`.

## Alternatives
- **Хранить детальные статусы только на клиенте.** Отвергнуто: Offline/Expired определяются по реакции Anthropic на стороне backend; клиент не имеет этой информации.
- **Заменить enum строкой произвольного статуса.** Отвергнуто: теряется валидация/типизация; enum с фиксированным набором безопаснее.
