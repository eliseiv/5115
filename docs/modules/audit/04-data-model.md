# Audit — Data Model

Владеет: `audit_logs`. Полный DDL — [03-data-model.md](../../03-data-model.md).

## audit_logs
| Поле | Тип | Назначение |
|---|---|---|
| `id` | UUID PK | |
| `user_id` | UUID FK | владелец события |
| `session_id` | UUID FK nullable | связь с сессией (если есть) |
| `event_type` | TEXT | каталог из 02-api-contracts |
| `payload` | JSONB | детали, без секретов |
| `created_at` | timestamptz | |

## Индексы
- `ix_audit_user_created (user_id, created_at DESC)`.
- `ix_audit_event_type (event_type, created_at DESC)`.

## Инварианты
- Append-only (app-level).
- `payload` без API-ключей/секретов/raw StoreKit.
- Только Audit-модуль пишет сюда.
