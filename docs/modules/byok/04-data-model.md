# BYOK — Data Model

Владеет: `byok_keys`. Полный DDL — [03-data-model.md](../../03-data-model.md).

## byok_keys
| Поле | Тип | Назначение |
|---|---|---|
| `user_id` | UUID PK | владелец |
| `encrypted_key` | BYTEA | AES-256-GCM ciphertext (+tag) пользовательского ключа |
| `encrypted_dek` | BYTEA | DEK, зашифрованный KMS |
| `nonce` | BYTEA | AES-GCM nonce |
| `key_status` | enum | valid / invalid / missing / validating / offline / expired ([ADR-016](../../adr/ADR-016-extended-byok-statuses.md)) |
| `enabled` | bool | использовать ли byok |
| `provider` | text NULL | провайдер ключа (`anthropic` / `openai`), определён детектором префиксов ([ADR-044](../../adr/ADR-044-multi-provider-byok.md), миграция `0013`). `NULL` = легаси-строка до миграции или нераспознанный формат → fallback-детект на генерации |
| `updated_at` | timestamptz | |

## Миграция 0013 ([ADR-044](../../adr/ADR-044-multi-provider-byok.md))
```sql
ALTER TABLE byok_keys ADD COLUMN provider TEXT NULL;
```
- Expand-only, **без backfill** (как `0009`/`0010`): легаси-строки → `provider=NULL`.
- `TEXT` (не enum) — расширяемость без `ALTER TYPE`; допустимые значения `{anthropic, openai}` валидируются приложением (детектор), не БД.
- Пишется при каждом `set_key` определённым детектором провайдером.

## Инварианты
- Plaintext ключ и plaintext DEK НИКОГДА не сохраняются.
- Только этот модуль читает/расшифровывает `byok_keys`.
- delete физически удаляет строку (зашифрованные материалы исчезают).
- `provider` отражает провайдера, определённого по ключу при последнем `set` ([ADR-044](../../adr/ADR-044-multi-provider-byok.md)); `activeModel`/статус отдаются без расшифровки ключа (читается `provider`, не plaintext).
