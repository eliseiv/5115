# Auth — Implementation Phases (backend scope)

Стек — [02-tech-stack.md](../../02-tech-stack.md) (Python 3.12 / FastAPI / SQLAlchemy async / PostgreSQL 16 / Alembic). Команды lint/format/typecheck/test — оттуда же.

## Phase 1 — Config и ключи
- Добавить в `src/app/config.py`:
  - `jwt_private_key` (`JWT_PRIVATE_KEY`), `jwt_private_key_path` (`JWT_PRIVATE_KEY_PATH`), `jwt_public_key_path` (`JWT_PUBLIC_KEY_PATH`) — `JWT_PUBLIC_KEY` уже есть.
  - `jwt_kid` (`JWT_KID`).
  - `auth_access_ttl_seconds` (`AUTH_ACCESS_TTL_SECONDS`, дефолт 3600), `auth_refresh_ttl_seconds` (`AUTH_REFRESH_TTL_SECONDS`, дефолт 2592000).
  - `auth_rate_limit_per_ip` (`AUTH_RATE_LIMIT_PER_IP`, дефолт 10), `auth_jwks_enabled` (`AUTH_JWKS_ENABLED`, дефолт `true`).
- Резолверы ключей: `resolve_private_key()` / `resolve_public_key()` — приоритет `*_PATH` (read file) > строка (`\n`-разэкранирование). Приватный ключ под redaction.
- **Не ломать** существующие `JWT_PUBLIC_KEY`/`JWT_JWKS_URL`/`JWT_ISSUER`/`JWT_AUDIENCE` (verify-path).

## Phase 2 — Миграция 0005
- `auth_devices`, `auth_refresh_tokens` ([04-data-model.md](04-data-model.md)). Expand-only, `down_revision='0004'`. `users` не трогать.

## Phase 3 — TokenIssuer + AuthService
- `src/app/auth/issuer.py` — RS256-подпись (claims `sub/device_id/iss/aud/iat/exp`, заголовок `kid`), reuse значений `JWT_ISSUER`/`JWT_AUDIENCE`.
- `src/app/auth/service.py` — find-or-create по `deviceId` (с гонко-безопасным `ON CONFLICT`), provisioning `users`, выпуск access+refresh, refresh-rotation/reuse-детект/ревокация.

## Phase 4 — Router
- `src/app/api_gateway/routers/auth.py` — `POST /register`, `POST /token`, `POST /refresh`, `GET /jwks` под `/v1/auth`.
- **Вне** `get_current_user`-зависимости; под per-IP rate-limit. `503` если приватный ключ не сконфигурирован.
- Подключить в основной app-роутинг (порядок middleware не нарушать: size→cid→[auth skip для /v1/auth]→rate-limit→handler).

## Phase 5 — Тесты ([06-testing-strategy.md](../../06-testing-strategy.md))
- Round-trip: `register` → выпущенный JWT проходит `JwtVerifier.verify()`.
- Идемпотентность: повторный `register`/`token` того же `deviceId` → тот же `userId`.
- Refresh rotation: старый инвалидируется, reuse → `401` + ревокация.
- Совместимость ADR-007: `sub` без `users`-строки провижинится на первом `/v1/*` (lazy fallback не сломан).
- PEM-в-env: оба механизма (`*_PATH` и `\n`-строка) дают рабочий issuer; отсутствие приватного → `503`.
- Rate-limit per IP на `/v1/auth/*`.

## Phase 6 — Sign in with Apple ([ADR-043](../../adr/ADR-043-sign-in-with-apple.md), закрывает [Q-018-2](../../99-open-questions.md))

Подробные указания для backend (НЕ код — контракт реализации). Образцы в коде: `JwtVerifier`/`PyJWKClient` (`src/app/api_gateway/auth.py`), test-mode alg-ветвление (`src/app/subscription/storekit.py`), `_find_or_create_identity`/`_issue_pair` (`src/app/auth/service.py`).

### 6.1 Config (`src/app/config.py`)
Добавить поля `Settings` (env, дефолты — [ADR-043 §3](../../adr/ADR-043-sign-in-with-apple.md)):
- `apple_oidc_issuer` (`APPLE_OIDC_ISSUER`, дефолт `https://appleid.apple.com`)
- `apple_jwks_url` (`APPLE_JWKS_URL`, дефолт `https://appleid.apple.com/auth/keys`)
- `apple_audience` (`APPLE_AUDIENCE`, дефолт `""`)
- `apple_test_mode` (`APPLE_TEST_MODE`, bool, дефолт `false`)
- `apple_test_secret` (`APPLE_TEST_SECRET`, дефолт `""`, секрет — redaction `*secret*`)
- Helper `apple_audience_resolved() -> str`: `apple_audience.strip()` если непуст, иначе `appstore_bundle_id.strip()`, иначе `""`.
- Переиспользовать существующий `jwks_cache_ttl_seconds` для кэша Apple JWKS (новый env НЕ вводить).

### 6.2 Верификатор `src/app/auth/apple.py`
- Доменный dataclass `VerifiedAppleIdentity(apple_sub: str, email: str | None, email_verified: bool)` (`email_verified` — строго `bool`, дефолт `false`).
- Класс `AppleIdentityVerifier` (singleton-фабрика как `get_jwt_verifier`/`get_storekit_verifier`):
  - `__init__` читает `apple_oidc_issuer`/`apple_jwks_url`/`apple_audience_resolved()`/test-флаги; создаёт `PyJWKClient(apple_jwks_url, cache_keys=True, lifespan=jwks_cache_ttl_seconds)`. `test_mode = apple_test_mode AND bool(apple_test_secret)`.
  - `verify(identity_token: str, nonce: str | None) -> VerifiedAppleIdentity`:
    - Распарсить JWS header, взять `alg` (образец `_jws_header` storekit).
    - `alg == "HS256"`: если НЕ `test_mode` → `UnauthorizedError` (401, fail-closed); иначе `jwt.decode(key=apple_test_secret, algorithms=["HS256"], options={"verify_aud": False, "require": ["sub"]})`.
    - иначе (RS256): резолв ключа `PyJWKClient.get_signing_key_from_jwt(token)` (ошибки `PyJWKClientError`/`httpx.HTTPError` → `401`); `jwt.decode(key=signing_key, algorithms=["RS256"], issuer=apple_oidc_issuer, audience=apple_audience_resolved(), options={"require": ["sub","iss","aud","exp"], "verify_aud": True})`. `jwt.InvalidTokenError` → `401`.
    - **nonce:** если `claims.get("nonce")` И `nonce` (request) непусты → если `sha256(nonce.encode()).hexdigest() != claims["nonce"]` → `401`. Иначе пропустить.
    - Вернуть `VerifiedAppleIdentity(apple_sub=str(claims["sub"]), email=claims.get("email"), email_verified=bool(claims.get("email_verified", False)))`.
  - **Аудитория не сконфигурирована** (`apple_audience_resolved() == ""`) → поднимать `ServiceUnavailableError` (503) — либо в verifier, либо в service до verify (выбрать одно место; [ADR-043 §1](../../adr/ADR-043-sign-in-with-apple.md)).
  - Токен/nonce НЕ логировать; в текст исключений не помещать.

### 6.3 Модель `auth_identities` + миграция `0012`
- DDL — [04-data-model.md §21](04-data-model.md), [03-data-model.md §21](../../03-data-model.md). Колонки `id`/`user_id`(FK CASCADE)/`provider`/`subject`/`email`/`created_at`; `UNIQUE(provider, subject)`; index по `user_id`.
- Миграция `revision="0012_auth_identities"`, `down_revision="0011_workspaces"` (ПОЛНЫЙ id, не короткий — иначе слом цепочки Alembic), single head, expand-only. `downgrade` — `drop_table`.
- Если используется ORM-модель — добавить таблицу в `src/app/models/tables.py` (как прочие auth-таблицы), не меняя существующие.

### 6.4 `AuthService.sign_in_with_apple(identity_token, device_id, nonce)`
Поток — [ADR-043 §5](../../adr/ADR-043-sign-in-with-apple.md), алгоритм — [03-architecture.md §Sign in with Apple](03-architecture.md#sign-in-with-apple-adr-043):
- `_require_issuer()` (НАШ issuer, иначе 503); резолв apple-аудитории (пусто → 503); `AppleIdentityVerifier.verify(...)`.
- `resolved_device_id = device_id or str(uuid4())`.
- Lookup `auth_identities (provider='apple', subject=apple_sub)` → найдено: `target=user_id`. Не найдено: `device_user=_find_or_create_identity(resolved_device_id)`; если у `device_user` нет apple-identity → `INSERT auth_identities(...)` + `target=device_user`; иначе `target=uuid4()` + `INSERT users(target)` + `INSERT auth_identities(target, ...)`.
- Upsert `auth_devices[resolved_device_id].user_id := target` (`INSERT ... ON CONFLICT (device_id) DO UPDATE SET user_id=EXCLUDED.user_id, last_seen_at=now()`).
- `_issue_pair(target, resolved_device_id)`; `commit`.
- Одна транзакция, idempotent, гонко-безопасно (`ON CONFLICT (provider, subject) DO NOTHING` + re-read на конкурентной вставке). `email` пишется при создании identity, на резолве НЕ перезаписывается.

### 6.5 Схема + роутер
- `src/app/schemas/auth.py`: `AppleSignInRequest(StrictModel)` — `identityToken: str` (`min_length=1`), `deviceId: DeviceId | None = None` (тот же тип), `nonce: str | None = None`. `extra='forbid'`.
- `src/app/api_gateway/routers/auth.py`: `POST /apple` под `/v1/auth`, **вне** `get_current_user`, под `_rate_limit` (как register); `auth.sign_in_with_apple(...)` → `_to_response(tokens)`.

### 6.6 Тесты (qa, [06-testing-strategy.md](../../06-testing-strategy.md))
- Test-mode HS256: валидный токен → пара; невалидная подпись/`exp` → `401`; HS256 при `APPLE_TEST_MODE=false` → `401`.
- Связывание: новый apple_sub + device без identity → привязка (тот же device-userId); повторный вход того же apple_sub (другой device) → тот же userId (кросс-девайс); device с занятой apple-identity → новый userId; конфликт apple_sub-user≠device-user → apple_sub-user, устройство upsert, без merge.
- nonce: claim+request совпадают → ok; не совпадают → `401`; нет claim → пропуск.
- Аудитория не задана (`APPLE_AUDIENCE`/`APPSTORE_BUNDLE_ID` пусты) → `503`.
- НАШ issuer не сконфигурирован → `503`. Rate-limit per IP. Idempotency повторного входа.
- Round-trip: выпущенный после Apple-входа access-token проходит `JwtVerifier.verify()`.

## Что НЕ делать
- Не менять `JwtVerifier.verify()` (НАШИ токены) и логику `register`/`token`/`refresh`/`jwks`.
- Не делать авто-merge данных (кошелёк/история) при конфликте — [Q-043-2](../../99-open-questions.md) (на MVP не реализуется).
- Не вводить email/пароль (out-of-scope, остаток [Q-018-2](../../99-open-questions.md)); не поддерживать Services ID / web-flow (только нативный bundle-aud, [Q-043-1](../../99-open-questions.md)).
- Не логировать `identityToken`/`nonce`; не хранить Apple-токен в БД.
- Не трогать admin-auth, preview, billing, tool-loop.
