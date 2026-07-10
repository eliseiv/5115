# API Gateway — API Contracts

Gateway не добавляет собственных бизнес-endpoint, кроме служебных. Бизнес-контракты — в документах соответствующих модулей. Здесь — сквозные правила и служебные endpoint.

## Сквозные правила запросов
- Заголовок `Authorization: Bearer <JWT>` обязателен для всех `/v1/*`, **кроме `/v1/auth/*`** (точка получения токена — выпуск через встроенный issuer, [ADR-018](../../adr/ADR-018-embedded-auth-issuer.md); защита — per-IP rate-limit) и `/v1/preview/*` (signed URL). Все прочие `/v1/*`, включая `GET /v1/tools` ([ADR-019](../../adr/ADR-019-tools-catalog-endpoint.md)), требуют JWT.
- Заголовок `X-Device-Id` опционален для `/v1/chat/*`. Он работает как override `device_id` для per-device rate limit; при отсутствии используется `device_id` из JWT-claim (fallback `x_device_id or current.device_id`). Если ни заголовка, ни claim нет — `device_id = None`, и per-device бакет лимита не применяется (остаются per-user и per-IP лимиты).
- Заголовок `X-Request-Id` опционален; если отсутствует — Gateway генерирует `requestId` (UUID) и возвращает в ответе `X-Request-Id`. Это **correlation id** одного HTTP-запроса (логи/трейсы). Он **НЕ** является ключом идемпотентности биллинга: идемпотентность credits-debit строится на `messageStepId` (см. [ADR-005](../../adr/ADR-005-idempotency-ledger.md), [chat-orchestrator](../chat-orchestrator/03-architecture.md)). Совпадение имени с публичным полем `requestId` контракта `/wallet/consume` не означает совпадения значений — в это поле Orchestrator кладёт `messageStepId`.
- `Content-Type: application/json` для POST.
- `userId` в теле обязан совпадать с `sub` JWT, иначе `403`.

## Карта маршрутов
| Метод | Путь | Модуль | Контракт |
|---|---|---|---|
| POST | /v1/auth/register, /v1/auth/token, /v1/auth/refresh | auth | [link](../auth/02-api-contracts.md) |
| GET | /v1/auth/jwks | auth | [link](../auth/02-api-contracts.md) |
| POST | /v1/chat/run | chat-orchestrator | [link](../chat-orchestrator/02-api-contracts.md) |
| POST | /v1/chat/tool-result | chat-orchestrator | [link](../chat-orchestrator/02-api-contracts.md) |
| GET | /v1/tools | chat-orchestrator | [link](../chat-orchestrator/02-api-contracts.md#get-v1tools--каталог-инструментов-adr-019) |
| GET | /v1/policy/effective | policy-engine | [link](../policy-engine/02-api-contracts.md) |
| GET | /v1/wallet | wallet-ledger | [link](../wallet-ledger/02-api-contracts.md) |
| POST | /v1/wallet/consume | wallet-ledger | [link](../wallet-ledger/02-api-contracts.md) |
| POST | /v1/subscription/sync | subscription | [link](../subscription/02-api-contracts.md) |
| POST | /v1/byok/set | byok | [link](../byok/02-api-contracts.md) |
| POST | /v1/byok/toggle | byok | [link](../byok/02-api-contracts.md) |
| POST | /v1/byok/delete | byok | [link](../byok/02-api-contracts.md) |
| GET PATCH DELETE | /v1/chats[/{id}] (+ /{id}/steps) | chats | [link](../chats/02-api-contracts.md) |
| GET | /v1/images (список), /v1/images/{imageId} (байты) | images ([ADR-058](../../adr/ADR-058-image-generation.md)) | [API-REFERENCE §27](../../API-REFERENCE.md) |
| GET PATCH | /v1/profile | profile | [link](../profile/02-api-contracts.md) |
| GET PATCH | /v1/preferences | preferences | [link](../preferences/02-api-contracts.md) |
| POST GET PATCH DELETE | /v1/workspaces[/{id}] (+ /{id}/files) | workspaces | [link](../workspaces/02-api-contracts.md) |
| GET POST PATCH DELETE | /v1/snippets[/{id}] | snippets | [link](../snippets/02-api-contracts.md) |
| ~~POST GET DELETE~~ | ~~/v1/attachments[/{id}]~~ | attachments — **отложен ([TD-015](../../100-known-tech-debt.md))** | MVP: inline base64 в `/v1/chat/run` ([ADR-020](../../adr/ADR-020-inline-base64-attachments-mvp.md)) |
| POST GET | /v1/tokens/purchase, /v1/tokens/products | token-purchase | [link](../token-purchase/02-api-contracts.md) |
| POST DELETE | /v1/notifications/device-token | notifications | [link](../notifications/02-api-contracts.md) |

> Расширение Figma-gap (2026-06-02): новые роуты модулей 10–17 (см. [figma-gap-analysis.md](../../figma-gap-analysis.md)). Все — под пользовательским JWT, изоляция по `sub`.
> **Вложения (2026-06-03, [ADR-020](../../adr/ADR-020-inline-base64-attachments-mvp.md)):** на MVP мультимодальный ввод — **inline base64 в `POST /v1/chat/run`** (`application/json`), отдельного `/v1/attachments`-роута **нет**. У `/v1/chat/run` повышенный transport size-лимит (`ATTACHMENT_REQUEST_BODY_LIMIT`, дефолт 12 MB) — **только** у этого роута; остальные сохраняют JSON `≤512KB` ([05-security.md](../../05-security.md)). Двухшаговый `POST /v1/attachments` (`multipart/form-data`) отложен ([ADR-014](../../adr/ADR-014-multimodal-attachments.md) → [TD-015](../../100-known-tech-debt.md)).

## Служебные endpoint
| Метод | Путь | Auth | Ответ |
|---|---|---|---|
| GET | /health | нет | `200 {status:"ok"}` |
| GET | /healthz | нет | `200 {status:"ok"}` — **алиас /health** (healthcheck Traefik/smoke, [ADR-017](../../adr/ADR-017-shared-server-traefik-deploy.md)) |
| GET | /ready | нет | `200 {db:"ok",redis:"ok"}` или `503` |
| GET | /metrics | scrape-токен/сеть | Prometheus exposition |

## Стандартный формат ошибки (4xx/5xx)
```json
{ "error": { "code": "validation_error", "message": "human readable", "requestId": "..." } }
```
`code` ∈ { `unauthorized`, `forbidden`, `not_found`, `conflict`, `payload_too_large`, `validation_error`, `rate_limited`, `internal_error`, `upstream_error` }.

> Бизнес-блокировки НЕ используют этот формат — они возвращают `200 {status:"blocked", blockReason}` (см. [ADR-004](../../adr/ADR-004-blocked-http-200.md)).

## HTTP-коды (технические)
| Код | Условие |
|---|---|
| 401 | нет/невалидный JWT |
| 403 | `userId != sub` |
| 404 | ресурс/сессия не найдены |
| 409 | конфликт идемпотентности (тот же ключ, другой payload) |
| 413 | превышен size-лимит |
| 422 | невалидная схема |
| 429 | превышен rate limit (жёсткий) |
| 5xx | внутренняя/upstream ошибка |

## OpenAPI / Swagger документация
Оформление автогенерируемой OpenAPI-документации (`/docs`, `/redoc`, `/openapi.json`) — на русском языке, с **двумя security schemes** (`bearerAuth` JWT для `/v1/*`, `adminToken` `X-Admin-Token` для `/v1/admin/*`), лаконичными user-facing текстами (без ADR/Q/TD-ссылок), тегами по модулям, описанием blocked-ответов и примерами. Swagger UI должен быть полностью рабочим для ручного тестирования (флоу register → Authorize → вызов). Полный стандарт и acceptance — [08-api-documentation.md](../../08-api-documentation.md). Отключение docs-endpoint в prod — env `DOCS_ENABLED` (см. [07-deployment.md](../../07-deployment.md#конфигурация-env)).
