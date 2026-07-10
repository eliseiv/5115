# API Gateway — Data Model

Gateway не владеет таблицами PostgreSQL.

## Redis-ключи
| Ключ | Назначение | TTL |
|---|---|---|
| `rl:user:<userId>` | счётчик rate limit per user | окно лимита |
| `rl:dev:<deviceId>` | rate limit per device | окно лимита |
| `rl:ip:<ip>` | rate limit per IP | окно лимита |
| `idem:<userId>:<idemKey>` | кратковременная in-flight метка идемпотентности списания (вспомогательная, не источник истины); `idemKey` = billing idempotency key — для chat-debit это `messageStepId`, **не** gateway correlation `requestId` | короткий (напр. 60s) |

> Источник истины идемпотентности — PostgreSQL unique index, см. [ADR-005](../../adr/ADR-005-idempotency-ledger.md). Redis — только ускорение. `idemKey` (billing) и gateway correlation `requestId` (`X-Request-Id`, логи/трейсы) — разные величины с разными жизненными циклами; не смешивать.
