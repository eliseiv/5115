# API Gateway — Architecture

## Middleware-цепочка (порядок)
1. **Size limit** — отсекает тело > лимита до парсинга (`413`).
2. **Correlation id** — `requestId` из `X-Request-Id` или генерация; кладётся в context-var, попадает во все логи/трейсы. Это исключительно correlation id одного HTTP-запроса; биллинг-идемпотентность к нему не привязана (она использует `messageStepId`, [ADR-005](../../adr/ADR-005-idempotency-ledger.md)).
3. **Auth (JWT) + lazy provisioning** — проверка подписи (RS256, по `JWT_PUBLIC_KEY`/`JWT_JWKS_URL`), `exp/iss/aud`; извлечение `sub`, `device_id` (`401`). Затем, **в `get_current_user` после успешной верификации и до downstream**, идемпотентный upsert строки `users` для `sub`: `INSERT INTO users (id) VALUES (:sub) ON CONFLICT (id) DO NOTHING` — гарантирует существование родителя для всех FK-зависимых вставок (race-free). Источник истины идентичности — **встроенный issuer** ([ADR-018](../../adr/ADR-018-embedded-auth-issuer.md), закрывает Q-005-1; verify-only внешний issuer сохраняется как опция), `users.id ≡ sub`. См. [ADR-007](../../adr/ADR-007-lazy-user-provisioning.md), [05-security.md](../../05-security.md#модель-идентичности-и-провижининг-пользователей).
   - **Исключение `/v1/auth/*`** ([ADR-018](../../adr/ADR-018-embedded-auth-issuer.md)): эти маршруты — точка выпуска токена, проходят **без** JWT-шага; их защищает per-IP rate-limit (шаг 4). `register` провижинит `users` **явно** (eager) тем же idempotent upsert; lazy-provisioning остаётся fallback для прочих путей. Двойной provisioning безопасен (`ON CONFLICT DO NOTHING`).
4. **Rate limit** — Redis sliding window per user/device/IP (`429`).
5. **Routing → handler** — Pydantic-валидация тела (`extra=forbid`, `422`); сверка `userId==sub` (`403`).
6. **Response mapping** — бизнес-200 vs тех. ошибки; redaction секретов в логах.
7. **Metrics/trace** — фиксация латентности, span.

```mermaid
flowchart LR
    REQ[Request] --> SZ[Size limit] --> CID[Correlation id] --> AUTH[JWT auth] --> RL[Rate limit] --> H[Handler/Router] --> RESP[Response mapping]
```

## Rate limiting
- Алгоритм: sliding window log / token bucket в Redis (ключи `rl:user:<id>`, `rl:dev:<id>`, `rl:ip:<addr>`).
- Лимиты из config/env (дефолты — [05-security.md](../../05-security.md), значения — [Q-003-1](../../99-open-questions.md)).

## Size-лимиты
- Глобальный body-лимит на ASGI-уровне (`SIZE_LIMIT_BODY`, дефолт 512 KB).
- Поле-специфичные лимиты (`message`, `context`, `result`) проверяются в Pydantic-валидаторах соответствующих схем.

### Per-path transport body-limit (`SizeLimitMiddleware._limit_for`)
`SizeLimitMiddleware` применяет общий `SIZE_LIMIT_BODY` ко всем путям, **кроме** upload-роутов с inline base64, которым нужен повышенный лимит (крупный файл в base64 превышает 512 KB). Метод `_limit_for(path)` выбирает лимит по пути:

| Правило сопоставления пути | Лимит (конфиг) | Дефолт | Роут | ADR |
|---|---|---|---|---|
| `path == "/v1/chat/run"` (точное) | `attachment_request_body_limit` (`ATTACHMENT_REQUEST_BODY_LIMIT`) | 12 MB | `POST /v1/chat/run` | [ADR-020](../../adr/ADR-020-inline-base64-attachments-mvp.md) |
| `path.startswith("/v1/workspaces/") and path.endswith("/files")` | `workspace_request_body_limit` (`WORKSPACE_REQUEST_BODY_LIMIT`) | 12 MB | `POST /v1/workspaces/{id}/files` | [ADR-045](../../adr/ADR-045-per-path-body-limit-workspace-files.md) |
| иначе | `size_limit_body` (`SIZE_LIMIT_BODY`) | 512 KB | все прочие | — |

**Точность правила workspace-files (важно):** префикс+суффикс матчит **именно** upload (`POST …/files`). НЕ задеты:
- CRUD `/v1/workspaces`, `/v1/workspaces/{id}` — нет суффикса `/files` → 512 KB (корректно, тела мелкие);
- `DELETE /v1/workspaces/{id}/files/{file_id}` — оканчивается на `/{file_id}`, не на `/files` → 512 KB;
- `GET /v1/workspaces/{id}/files` (список) — оканчивается на `/files`, попадает под повышенный лимит, но безвредно (GET-тело пустое). Матч метод-агностичен (как и `/v1/chat/run`); проверка метода не вводится — единственный путь с непустым телом под суффиксом `/files` — POST upload.

**Инвариант источника истины** ([ADR-045 §1](../../adr/ADR-045-per-path-body-limit-workspace-files.md)): `WORKSPACE_REQUEST_BODY_LIMIT ≥ WORKSPACE_FILE_MAX_BYTES*4/3 + JSON-запас(≥256 KB)`. Per-file потолок — единственный источник истины (`WORKSPACE_FILE_MAX_BYTES`=8 MB, [ADR-036 §4](../../adr/ADR-036-workspaces-implementation.md)); транспортный лимит производен от него (симметрично `ATTACHMENT_MAX_BYTES_DOCUMENT` ↔ `ATTACHMENT_REQUEST_BODY_LIMIT`). Memory-DoS guard сохранён: повышение точечно, прикладной size-cap (`validate_and_extract`) режет до 8 MB **до** base64-decode. Реализация — `src/app/api_gateway/middleware.py::SizeLimitMiddleware._limit_for`. Остаточная `Content-Length`-зависимость — [TD-017](../../100-known-tech-debt.md).

## Зависимости реализации
- FastAPI dependencies: `get_current_user`, `get_db`, `get_redis`, `require_owner`.
- Без бизнес-логики: handler делегирует в use-case модуля.
