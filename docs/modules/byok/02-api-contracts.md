# BYOK — API Contracts

Общий формат ответа для всех трёх endpoint (расширен [ADR-016](../../adr/ADR-016-extended-byok-statuses.md)):
```json
{
  "byokEnabled": false,
  "keyStatus": "missing | validating | valid | invalid | offline | expired",
  "activeModel": "claude-sonnet-4-6 | null"
}
```
> **Расширение [ADR-016](../../adr/ADR-016-extended-byok-statuses.md) (обратно совместимо):** к исходным `valid|invalid|missing` добавлены `validating` (Checking), `offline` (сетевая ошибка валидации, не 401), `expired` (был valid, отозван/истёк — обнаружено при использовании). Новое опциональное поле `activeModel` — активная модель при `keyStatus=valid` (иначе `null`), источник — `BYOK_DEFAULT_MODEL`/подтверждённая при валидации. Клиенты, знающие только 3 старых статуса, продолжают работать (новые статусы трактуются как «не valid»).

**Маппинг дизайн ↔ keyStatus:** Not set → `missing`; Checking → `validating`; Connected+Active(+модель) → `valid` + `activeModel`; Invalid(401) → `invalid`; Offline(network) → `offline`; Expired(revoked) → `expired`.

## POST /v1/byok/set
### Request
```json
{ "userId": "uuid", "apiKey": "string (Anthropic sk-ant-… ИЛИ OpenAI sk-…/sk-proj-…)" }
```
- `apiKey` — ключ **любого поддерживаемого провайдера** ([ADR-044](../../adr/ADR-044-multi-provider-byok.md)), независимо от `LLM_PROVIDER` инстанса. Никогда не логируется; size-лимит маленький (≤ 4KB).

### Поведение ([ADR-044](../../adr/ADR-044-multi-provider-byok.md))
- **Детект провайдера по ключу:** `sk-ant-`→anthropic (раньше `sk-`); `sk-proj-`/`sk-`→openai; иначе → формат не распознан → `key_status=invalid` **без сетевого вызова**.
- Генерация DEK → AES-256-GCM шифрование ключа → KMS encrypt DEK → upsert `byok_keys` (+ колонка `provider` = определённый провайдер).
- **Валидация ключа лёгким вызовом провайдера, определённого по ключу** (Anthropic `messages.create(max_tokens=1)` / OpenAI `models.list`) — НЕ провайдера инстанса. Переходы статуса ([ADR-016](../../adr/ADR-016-extended-byok-statuses.md)): `missing → validating → (valid | invalid | offline)`. 401 → `invalid`; сетевая ошибка (не 401) → `offline`; успех → `valid` (+ `activeModel` = BYOK-дефолт определённого провайдера).
- При невалидном/offline ключе: сохранить (зашифрованно) с соответствующим статусом и вернуть его (UI покажет ошибку/ретрай). `byokEnabled` не включается автоматически.

### Response (200)
`{ byokEnabled, keyStatus }`.

## POST /v1/byok/toggle
### Request
```json
{ "userId": "uuid", "enabled": true }
```
### Поведение
- Нельзя включить (`enabled=true`), если `keyStatus != valid` (включая `validating`/`offline`/`expired`) → возвращает `byokEnabled=false, keyStatus` (не включает) или `409`. Дефолт: вернуть текущий статус без включения, без ошибки.
### Response (200)
`{ byokEnabled, keyStatus }`.

## POST /v1/byok/delete
### Request
```json
{ "userId": "uuid" }
```
### Поведение
- Удаляет `byok_keys` строку (зашифрованные материалы) → `keyStatus=missing`, `byokEnabled=false`.
### Response (200)
`{ byokEnabled: false, keyStatus: "missing" }`.

## Инварианты
- Ответы НИКОГДА не содержат plaintext ключ или его части.
- `apiKey` не попадает в логи/audit/трейсы (redaction).
- `activeModel` — не секрет (имя модели), безопасно отдавать; присутствует только при `keyStatus=valid`; значение = BYOK-дефолт **провайдера, определённого по ключу** ([ADR-044](../../adr/ADR-044-multi-provider-byok.md)).
- При использовании ключа в `/chat/run` (mode=byok): 401 от **провайдера ключа** для ранее `valid` ключа → перевод в `expired` ([ADR-016](../../adr/ADR-016-extended-byok-statuses.md)); сетевая ошибка статус не меняет (транзиентно).
- Контракт ответа (`{byokEnabled, keyStatus, activeModel}`) **не расширяется** колонкой `provider` (внутреннее поле строки); iOS определяет провайдера по формату ключа/`activeModel`.
