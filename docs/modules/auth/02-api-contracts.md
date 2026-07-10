# Auth — API Contracts

Все эндпоинты под префиксом `/v1/auth`. **Без** пользовательского JWT (это точка его получения). `Content-Type: application/json` для POST. Тело — строгая Pydantic-схема (`extra='forbid'`). Защита — rate-limit per IP ([05-security.md](05-security.md)).

## `POST /v1/auth/register`
Создать/найти идентичность устройства и выдать токены.

**Request**
```json
{ "deviceId": "A1B2C3D4-..." }
```
- `deviceId` — опционально, строка `1..128`, charset `[A-Za-z0-9._:-]`. Если отсутствует/пусто — backend генерирует UUIDv4 и возвращает его.

**Response 200**
```json
{
  "userId": "550e8400-e29b-41d4-a716-446655440000",
  "deviceId": "A1B2C3D4-...",
  "accessToken": "<RS256 JWT>",
  "tokenType": "Bearer",
  "expiresIn": 3600,
  "refreshToken": "<opaque>",
  "refreshExpiresIn": 2592000
}
```
- Если `deviceId` уже известен (`auth_devices`) — возвращается **существующий** `userId` (идемпотентно); новый `userId` не создаётся.
- Новое устройство → новый `userId = uuid4()`, provisioning `users`, строка `auth_devices`.
- `accessToken` claims: `sub=userId`, `device_id=deviceId`, `iss`, `aud`, `iat`, `exp`, `kid` (заголовок). Верифицируется собственным `JwtVerifier`.

**Коды:** `200` ok; `422` невалидный `deviceId`; `429` rate-limit; `503` issuer не сконфигурирован (нет приватного ключа).

## `POST /v1/auth/token`
Повторно получить токены для **известного** устройства (без создания новой идентичности).

**Request**
```json
{ "deviceId": "A1B2C3D4-..." }
```
- `deviceId` — **обязателен**.

**Response 200** — как у `register` (та же схема).
- Если устройство неизвестно → создаётся как в `register` (find-or-create; `token` и `register` различаются лишь обязательностью `deviceId` и семантическим намерением «у меня уже есть устройство»).

**Коды:** `200`; `422` отсутствует/невалидный `deviceId`; `429`; `503`.

## `POST /v1/auth/refresh`
Обменять refresh-token на новую пару (rotation, single-use).

**Request**
```json
{ "refreshToken": "<opaque>" }
```

**Response 200** — новая пара (как `register`, тот же `userId`/`deviceId` из связанной записи).

**Поведение:**
- Предъявленный refresh-token **инвалидируется** (single-use); выдаётся новый.
- Reuse уже использованного/неизвестного/истёкшего refresh → `401`; при детекте reuse валидной-но-использованной записи — ревокация **всей цепочки** устройства (анти-кража).
- Хранение — `token_hash` (SHA-256), не plaintext ([04-data-model.md](04-data-model.md)).

**Коды:** `200`; `401` невалидный/истёкший/reused refresh; `422` отсутствует поле; `429`.

## `POST /v1/auth/apple`
Sign in with Apple ([ADR-043](../../adr/ADR-043-sign-in-with-apple.md), закрывает [Q-018-2](../../99-open-questions.md)). Клиент шлёт Apple **identity token** (OIDC JWT, RS256, нативный flow), backend верифицирует и выдаёт **НАШУ** пару токенов (как `register`). Даёт кросс-девайс аккаунт.

**Request**
```json
{ "identityToken": "<Apple OIDC JWT>", "deviceId": "A1B2C3D4-...", "nonce": "<raw nonce>" }
```
- `identityToken` — **обязателен**, непустая строка. Никогда не логируется.
- `deviceId` — опционален, тип как у `register` (`1..128`, charset `[A-Za-z0-9._:-]`). Отсутствует/пуст → backend генерирует UUIDv4 и возвращает его.
- `nonce` — опционален, raw-nonce, переданный клиентом Apple. Если в токене есть claim `nonce` и клиент прислал `nonce` → проверяется `sha256(nonce)==claim` (иначе `401`). Никогда не логируется.

**Верификация** ([ADR-043 §2](../../adr/ADR-043-sign-in-with-apple.md)): `iss=https://appleid.apple.com` (`APPLE_OIDC_ISSUER`), `aud`=bundle id (`APPLE_AUDIENCE`, фолбэк `APPSTORE_BUNDLE_ID`), RS256-подпись по Apple JWKS (`APPLE_JWKS_URL`, кэш `jwks_cache_ttl_seconds`), обязательны claims `sub`/`iss`/`aud`/`exp`. Test-mode (HS256) — только при `APPLE_TEST_MODE=true`+`APPLE_TEST_SECRET` (герметичные тесты).

**Response 200** — `TokenResponse` (та же схема, что `register`). Какой `userId` вернётся — определяет связывание:
- `apple_sub` известен (`auth_identities`) → его `userId` (кросс-девайс, тот же аккаунт).
- `apple_sub` неизвестен → привязать к текущему device-аккаунту, если у него **нет** Apple-идентичности (сохраняет кредиты/историю); иначе создать нового пользователя.
- Конфликт `apple_sub`-user ≠ device-user → берём `apple_sub`-user, устройство upsert на него; авто-merge данных **не** выполняется ([Q-043-2](../../99-open-questions.md)).

**Коды:** `200` ok; `401` невалидный/просроченный токен, неверный `iss`/`aud`/подпись, нет обязательных claims, несовпадение nonce; `422` нарушение схемы запроса; `429` rate-limit; `503` НАШ issuer не сконфигурирован **или** Apple-аудитория не задана (и `APPLE_AUDIENCE`, и `APPSTORE_BUNDLE_ID` пусты).

## `GET /v1/auth/jwks`
JWKS с публичным ключом (для самопроверки/отладки/будущих верификаторов). **Опционально-публичный** (не содержит секретов). Управляется тем же `DOCS_ENABLED`-классом видимости — конкретно env `AUTH_JWKS_ENABLED` (дефолт `true`).

**Response 200**
```json
{ "keys": [ { "kty": "RSA", "use": "sig", "alg": "RS256", "kid": "<JWT_KID>", "n": "...", "e": "AQAB" } ] }
```
- Только публичный ключ. Приватный никогда не отдаётся.

**Коды:** `200`; `404` если `AUTH_JWKS_ENABLED=false` или публичный ключ не сконфигурирован.

## Ошибки
Стандартный формат ([api-gateway/02-api-contracts.md](../api-gateway/02-api-contracts.md)): `{ "error": { "code", "message", "requestId" } }`. `code` ∈ { `validation_error`, `unauthorized`, `rate_limited`, `internal_error` } + `503` (`code=service_unavailable`) при несконфигурированном issuer.

## Карта (для api-gateway)
| Метод | Путь | Auth | Назначение |
|---|---|---|---|
| POST | /v1/auth/register | нет (rate-limit per IP) | find-or-create identity + токены |
| POST | /v1/auth/token | нет (rate-limit per IP) | токены для известного устройства |
| POST | /v1/auth/refresh | нет (rate-limit per IP) | rotation refresh → новая пара |
| POST | /v1/auth/apple | нет (rate-limit per IP) | Sign in with Apple → НАША пара (кросс-девайс), [ADR-043](../../adr/ADR-043-sign-in-with-apple.md) |
| GET | /v1/auth/jwks | нет (опц. публичный) | публичный ключ (JWKS) |
