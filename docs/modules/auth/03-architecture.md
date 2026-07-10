# Auth — Architecture

## Компоненты
- **`TokenIssuer`** (новый, `src/app/auth/issuer.py`) — подписывает RS256 JWT приватным ключом; собирает claims (`sub`, `device_id`, `iss`, `aud`, `iat`, `exp`), ставит `kid` в заголовок.
- **`AuthService`** (новый, `src/app/auth/service.py`) — find-or-create identity по `deviceId`, provisioning `users`, выпуск access+refresh, refresh-rotation/ревокация.
- **Router** (`src/app/api_gateway/routers/auth.py`) — `register`/`token`/`refresh`/`apple`/`jwks`, вне JWT-зависимости, под per-IP rate-limit.
- **`JwtVerifier`** (существующий, `src/app/api_gateway/auth.py`) — **не меняется**; верифицирует выпущенные токены. Issuer и verifier берут ключи из одного config.
- **`AppleIdentityVerifier`** (новый, `src/app/auth/apple.py`, [ADR-043](../../adr/ADR-043-sign-in-with-apple.md)) — верифицирует Apple identity token (OIDC RS256 по Apple JWKS, `iss`/`aud`/`exp`/`sub`); alg-ветвление HS256→test-mode / RS256→real (образец `StoreKitVerifier` + `JwtVerifier`/`PyJWKClient`). Возвращает `VerifiedAppleIdentity(apple_sub, email?, email_verified: bool)` (`email_verified` строго `bool`, дефолт `false`). **Это верификатор внешнего (Apple) токена для ВХОДА; НАШИ токены по-прежнему выпускает `TokenIssuer` и верифицирует `JwtVerifier`.**

## Выпуск access-token
1. Резолв `userId` по `deviceId` (см. ниже).
2. Claims: `sub=userId`, `device_id=deviceId`, `iss=JWT_ISSUER`, `aud=JWT_AUDIENCE`, `iat=now`, `exp=now+AUTH_ACCESS_TTL_SECONDS`. Заголовок `kid=JWT_KID`.
3. Подпись `RS256` приватным ключом → JWT.

## Find-or-create identity
```text
device = SELECT * FROM auth_devices WHERE device_id = :deviceId
if device exists:
    userId = device.user_id
else:
    userId = uuid4()
    INSERT INTO users (id) VALUES (:userId) ON CONFLICT (id) DO NOTHING   -- провижининг (ADR-007), идемпотентно
    INSERT INTO auth_devices (user_id, device_id) VALUES (:userId, :deviceId)
        ON CONFLICT (device_id) DO NOTHING                                 -- race: concurrent register того же deviceId
    -- при ON CONFLICT повторно прочитать строку, чтобы взять победивший userId
```
> Гонка двух одновременных `register` одного `deviceId`: `UNIQUE(device_id)` + `ON CONFLICT DO NOTHING` + повторное чтение → оба вернут один `userId`. Без race by construction.

## Sign in with Apple ([ADR-043](../../adr/ADR-043-sign-in-with-apple.md))
`AuthService.sign_in_with_apple(identity_token, device_id, nonce)` — один атомарный поток:
```text
require НАШ issuer (нет приватного ключа → 503)
resolve apple-аудиторию (APPLE_AUDIENCE → фолбэк APPSTORE_BUNDLE_ID; пусто → 503)
apple_sub, email = AppleIdentityVerifier.verify(identity_token, nonce)   -- ошибка → 401
resolved_device_id = device_id or uuid4()
identity = SELECT * FROM auth_identities WHERE provider='apple' AND subject=apple_sub
if identity exists:
    target = identity.user_id                                            -- кросс-девайс: тот же аккаунт
else:
    device_user = _find_or_create_identity(resolved_device_id)           -- переиспользуем, idempotent/race-safe
    if device_user НЕ имеет apple-идентичности:
        INSERT auth_identities(device_user, 'apple', apple_sub, email)    -- привязка, сохраняет кредиты/историю
        target = device_user
    else:
        target = uuid4(); INSERT users(target); INSERT auth_identities(target,'apple',apple_sub,email)
upsert auth_devices[resolved_device_id].user_id := target               -- конфликт apple_sub-user≠device-user → берём apple_sub-user (target), без авто-merge данных (Q-043-2)
_issue_pair(target, resolved_device_id); commit                         -- НАША пара токенов (как register)
```
> Idempotent/гонко-безопасно: `UNIQUE(provider, subject)` + `ON CONFLICT DO NOTHING` + повторное чтение (как `auth_devices`). Повторный вход того же `apple_sub` → тот же `userId`. `email` пишется при создании identity, на резолве не перезаписывается ([Q-043-1](../../99-open-questions.md)).

## Refresh-token (rotation)
- Opaque = `secrets.token_urlsafe(32)`; в БД хранится `sha256(token)` (`auth_refresh_tokens.token_hash`), TTL = `AUTH_REFRESH_TTL_SECONDS`.
- `refresh`: lookup по `token_hash` → если найден, не использован, не истёк → пометить `used_at=now`, выдать новую пару (новая строка refresh, ссылается на ту же `(user_id, device_id)`).
- **Reuse-детект:** предъявлен `token_hash` с непустым `used_at` → кража; ревокация всей цепочки устройства (`UPDATE ... SET revoked_at=now WHERE user_id=? AND device_id=?`), ответ `401`.

## Согласование с провижинингом ([ADR-007](../../adr/ADR-007-lazy-user-provisioning.md))
- `register` провижинит `users` **явно** (eager) — тем же idempotent upsert, что и gateway.
- Lazy-provisioning в `get_current_user` **остаётся** fallback: токен с `sub` без строки `users` всё равно провижинится на первом `/v1/*`. Два пути не конфликтуют (`ON CONFLICT DO NOTHING`).
- `trial_used`/policy: `users` создаётся строго с DDL-дефолтами — биллинг видит нового пользователя идентично прежнему (внешний issuer) поведению.

## Ключи и config
См. [05-security.md](05-security.md). Issuer и verifier читают одну пару:
- Приватный: `JWT_PRIVATE_KEY_PATH` (файл) > `JWT_PRIVATE_KEY` (PEM-строка с `\n`-экранированием). Нет ни того, ни другого → issuer-эндпоинты `503` (verify-only режим работает).
- Публичный: `JWT_PUBLIC_KEY_PATH` > `JWT_PUBLIC_KEY`. Используется `JwtVerifier` (verify) и `jwks` (отдача).
- `JWT_KID` — идентификатор ключа для `kid`/JWKS (ротация ключей — future, [05-security.md](05-security.md)).

## Что НЕ изменяется
- `JwtVerifier.verify()` — логика верификации без правок.
- Admin-auth, preview, billing, tool-loop — не затрагиваются.
- Внешний-issuer режим (`JWT_JWKS_URL`) сохраняется как опция (verify-only). **Sign in with Apple ([ADR-043](../../adr/ADR-043-sign-in-with-apple.md)) реализован НЕ через этот режим** — Apple-токен используется только для входа (`POST /v1/auth/apple`), далее выпускается НАША пара ([Q-018-2](../../99-open-questions.md) закрыт).
