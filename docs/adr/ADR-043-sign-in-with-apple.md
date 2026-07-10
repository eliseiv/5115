# ADR-043 — Sign in with Apple (`POST /v1/auth/apple`)

- Статус: Accepted
- Дата: 2026-06-19
- Расширяет: [ADR-018](ADR-018-embedded-auth-issuer.md) (встроенный auth-issuer, device-based)
- Закрывает: [Q-018-2](../99-open-questions.md) (апгрейд идентичности — Apple Sign-In, кросс-девайс аккаунт)
- Тип: implementation-ADR (новый эндпоинт + новая таблица идентичностей, миграция `0012`)

> **Ревизия 2026-06-19 (post-implementation sync, docs↔код):** §2 выровнен к реализации `src/app/auth/apple.py`: доменный результат именуется `VerifiedAppleIdentity` (не `AppleIdentity`); поле `email_verified` — строго `bool` с дефолтом `false` (`bool(claims.get("email_verified", False))`), а не `bool | None` (строго безопаснее: отсутствие/неверный claim → `false`). Контракт эндпоинта, коды (`401`/`503`), связывание и миграция `0012` — неизменны.

## Контекст

[ADR-018](ADR-018-embedded-auth-issuer.md) дал **device-based** анонимную идентичность: `deviceId → userId`, выпуск НАШЕЙ пары токенов (RS256 access + opaque refresh). Ограничение зафиксировано там же ([ADR-018 §Consequences](ADR-018-embedded-auth-issuer.md)): при потере устройства / переустановке без сохранённого refresh-token пользователь получает **новый** `userId` (потеря истории/баланса). Кросс-девайс аккаунт и account-recovery были оставлены как [Q-018-2](../99-open-questions.md).

Пользователь выбрал **Sign in with Apple** как путь апгрейда. iOS получает от Apple **identity token** (OIDC JWT, RS256) при нативном Sign in with Apple и отправляет его на backend. Backend верифицирует токен, сопоставляет Apple-идентичность с НАШИМ `userId` и выдаёт НАШУ пару токенов — тот же контракт ответа, что и `register`/`token` ([ADR-018](ADR-018-embedded-auth-issuer.md)). Это даёт кросс-девайс аккаунт: один и тот же `apple_sub` на разных устройствах резолвится в один `userId`.

Apple identity token при **нативном** Sign in with Apple: `iss=https://appleid.apple.com`, `aud` = **bundle id** приложения (НЕ Services ID — тот только для web-flow), `sub` = стабильный Apple user identifier, RS256, JWKS на `https://appleid.apple.com/auth/keys`. `email`/`email_verified` — опциональны (приходят при первом согласии, могут быть private-relay).

## Решение

### 1. Контракт `POST /v1/auth/apple`

- Путь: `POST /v1/auth/apple`, рядом с `register`/`token`/`refresh` в роутере `src/app/api_gateway/routers/auth.py`.
- **Public** (без пользовательского JWT — это точка получения токена), под `enforce_auth_limits(ip)` (тот же per-IP rate-limit, что и остальные `/v1/auth/*`).
- Request (строгая `StrictModel`, `extra='forbid'`):
  ```json
  { "identityToken": "<Apple OIDC JWT, RS256>", "deviceId": "A1B2C3D4-...", "nonce": "<raw nonce>" }
  ```
  - `identityToken` — обязателен, непустая строка. **Никогда не логируется** (см. §6).
  - `deviceId` — опционален, тот же тип `DeviceId` (`1..128`, charset `[A-Za-z0-9._:-]`). Отсутствует/пуст → backend генерирует UUIDv4 и возвращает его (как `register`).
  - `nonce` — опционален, raw-nonce, который клиент передал Apple при запросе токена (см. §2, nonce-политика). **Никогда не логируется** (см. §6).
- Response `200` — `TokenResponse` (та же схема, что `register`/`token`/`refresh`): `userId`, `deviceId`, `accessToken`, `tokenType=Bearer`, `expiresIn`, `refreshToken`, `refreshExpiresIn`. Связывание определяет, какой `userId` вернётся (§5).
- Коды ошибок:
  - `401` (`unauthorized`) — невалидный / просроченный токен, неверный `iss`/`aud`/подпись, отсутствие обязательных claims (`sub`/`iss`/`aud`/`exp`), несовпадение nonce. **Fail-closed.**
  - `422` (`validation_error`) — нарушение схемы запроса (пустой `identityToken`, невалидный `deviceId`, лишние поля).
  - `429` (`rate_limited`) — per-IP лимит.
  - `503` (`service_unavailable`) — НАШ issuer не сконфигурирован (нет приватного ключа подписи — `_issue_pair`/`register_or_token` уже так делают, [ADR-018 §7](ADR-018-embedded-auth-issuer.md)) **или** Apple-аудитория не сконфигурирована (см. §3, «not configured» → `503`).

> Решение `503` (а не `401`) для несконфигурированной Apple-аудитории: это операционная мисконфигурация инстанса (как несконфигурированный issuer), не ошибка клиента. Согласовано с `503` существующих `/v1/auth/*` при отсутствующем приватном ключе.

### 2. Верификатор `src/app/auth/apple.py::AppleIdentityVerifier`

Новый модуль-верификатор по образцу `JwtVerifier` (`src/app/api_gateway/auth.py`, `PyJWKClient` + кэш) и test-mode-паттерна `StoreKitVerifier` (`src/app/subscription/storekit.py`, alg-ветвление HS256→test).

Возвращает доменный результат `VerifiedAppleIdentity(apple_sub: str, email: str | None, email_verified: bool)` (поле `email_verified` — строго `bool`, дефолт `false` при отсутствии claim: `bool(claims.get("email_verified", False))`).

**Alg-ветвление по заголовку JWS `alg`** (как `StoreKitVerifier.verify`):

- `alg=HS256` → **test-branch**, активна **ТОЛЬКО** когда `APPLE_TEST_MODE=true` И `APPLE_TEST_SECRET` непуст (оба условия). Иначе — `401` (fail-closed, как `StoreKitVerifier`: HS256 вне test-mode → ошибка верификации). `jwt.decode(token, key=APPLE_TEST_SECRET, algorithms=["HS256"], options={"verify_aud": False, "require": ["sub"]})` → извлечь `sub`/`email`/`email_verified`. Для герметичных тестов без Apple-инфры.
- любой другой `alg` (на практике `RS256`) → **real-branch, ВСЕГДА** (никогда не ослабляется test-mode'ом):
  - `PyJWKClient(APPLE_JWKS_URL, cache_keys=True, lifespan=jwks_cache_ttl_seconds)` (переиспользуем существующий `jwks_cache_ttl_seconds=300`) → `get_signing_key_from_jwt(token)`. Ошибки резолва ключа (`PyJWKClientError`/`httpx.HTTPError`) → `401`.
  - `jwt.decode(token, key=signing_key, algorithms=["RS256"], issuer=APPLE_OIDC_ISSUER, audience=<apple-aud>, options={"require": ["sub", "iss", "aud", "exp"], "verify_aud": True})`. Любой `jwt.InvalidTokenError` → `401`.
  - `<apple-aud>` = разрешённая аудитория (§3).

**nonce-политика (опциональна):**
- Если в claims токена есть `nonce` И клиент прислал `nonce` в запросе → провалидировать `sha256(request.nonce).hexdigest() == claims["nonce"]` (Apple кладёт в claim **хэш** nonce при нативном flow). Несовпадение → `401`.
- Если claim `nonce` отсутствует ИЛИ клиент не прислал `nonce` → nonce не проверяется (опционально на MVP, не ужесточаем). Усиление (обязательный nonce, anti-replay store) — [Q-043-1](../99-open-questions.md).

> nonce-политика «опциональна, но проверяется при наличии обеих сторон» — компромисс MVP: не ломает клиентов, не присылающих nonce, но при использовании nonce защищает от replay перехваченного токена. Сравнение nonce — обычное равенство строк (хэши, не секреты для constant-time).

Identity token **никогда не логируется** (redaction покрывает `*token*`; verifier не пишет токен в логи/исключения).

### 3. Конфиг `APPLE_*` (`src/app/config.py`)

Новые поля `Settings` (env, не секреты кроме `APPLE_TEST_SECRET`):

| Поле | Env | Дефолт | Назначение |
|---|---|---|---|
| `apple_oidc_issuer` | `APPLE_OIDC_ISSUER` | `https://appleid.apple.com` | Ожидаемый `iss` Apple-токена. |
| `apple_jwks_url` | `APPLE_JWKS_URL` | `https://appleid.apple.com/auth/keys` | JWKS Apple для `PyJWKClient`. |
| `apple_audience` | `APPLE_AUDIENCE` | `""` | Ожидаемый `aud` = **bundle id** приложения. Пусто → фолбэк на `APPSTORE_BUNDLE_ID`. |
| `apple_test_mode` | `APPLE_TEST_MODE` | `false` | Включает HS256 test-branch (только с непустым `APPLE_TEST_SECRET`). |
| `apple_test_secret` | `APPLE_TEST_SECRET` | `""` | HS256-секрет для герметичных тестов. **Секрет** (redaction: `*secret*`). |

- **Кэш JWKS** — переиспользуется существующий `jwks_cache_ttl_seconds` (`JWT_JWKS_CACHE_TTL=300`); отдельный env не вводится.
- **Резолв аудитории:** helper `Settings.apple_audience_resolved() -> str`: вернуть `apple_audience.strip()` если непуст, иначе `appstore_bundle_id.strip()`, иначе `""`. Пусто → эндпоинт «not configured» → `503` (см. §1). Per-instance `APPLE_AUDIENCE` = реальный bundle инстанса (broadnova `com.lor.5075claude` / avelyra `com.nad.5112claude` / orvianix `com.ari.5108codex`); фолбэк на `APPSTORE_BUNDLE_ID` означает «если bundle уже задан для StoreKit — он же годится как Apple-аудитория».

> Per-provider/per-instance: значения берутся из `.env` инстанса (как `APPSTORE_BUNDLE_ID`). Провайдер LLM (Anthropic|OpenAI) на это не влияет — фича провайдер-агностична.

### 4. Идентичность + миграция `0012`

Новая таблица `auth_identities` (внешние identity-провайдеры, расширяемо на email/Google и т.п.):

```sql
CREATE TABLE auth_identities (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider    TEXT NOT NULL,                       -- 'apple' (расширяемо)
    subject     TEXT NOT NULL,                       -- провайдерский стабильный id (apple sub)
    email       TEXT,                                -- опционально (может быть private-relay)
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ux_auth_identities_provider_subject ON auth_identities (provider, subject);
CREATE INDEX ix_auth_identities_user ON auth_identities (user_id);
```

- `UNIQUE(provider, subject)` — точка кросс-девайс резолва (один Apple-аккаунт = один `userId`) и гонко-безопасности (`ON CONFLICT (provider, subject) DO NOTHING` + повторное чтение, как `auth_devices`).
- `ix_auth_identities_user` — обратный lookup «есть ли у `userId` Apple-идентичность» (для связывания, §5).
- FK `ON DELETE CASCADE` (идентичности живут пока живёт `users`).
- Миграция `0012` (expand-only): `revision="0012_auth_identities"`, `down_revision="0011_workspaces"` (полный id), **single head**. `users`/`auth_devices`/`auth_refresh_tokens` НЕ меняются.

### 5. Логика `AuthService.sign_in_with_apple(identity_token, device_id, nonce)`

Один атомарный поток (одна транзакция запроса, idempotent, гонко-безопасный):

1. `_require_issuer()` — НАШ issuer должен быть сконфигурирован, иначе `503` (как `register_or_token`).
2. Резолв Apple-аудитории (§3); пусто → `503` («not configured»).
3. `AppleIdentityVerifier.verify(identity_token, nonce)` → `apple_sub` (+ `email`/`email_verified`). Ошибка → `401`.
4. Резолв `deviceId`: `resolved_device_id = device_id or str(uuid4())` (как `register_or_token`).
5. **Lookup** `auth_identities` по `(provider='apple', subject=apple_sub)`:
   - **Найдено** → `target_user_id = identity.user_id` (кросс-девайс: тот же аккаунт).
   - **Не найдено** → определить, можно ли привязать к текущему device-аккаунту:
     - Резолв `device_user_id` по `resolved_device_id` (через `_find_or_create_identity`, переиспользуем — создаёт `users`+`auth_devices` для нового устройства, idempotent/race-safe).
     - Если у `device_user_id` **НЕТ** строки в `auth_identities` с `provider='apple'` (любой subject) → **привязать к нему**: `INSERT auth_identities(user_id=device_user_id, provider='apple', subject=apple_sub, email)` → `target_user_id = device_user_id`. Сохраняет кредиты/историю анонимного device-аккаунта.
     - Если у `device_user_id` **УЖЕ ЕСТЬ** Apple-идентичность (с другим subject) → device-аккаунт уже занят другим Apple-аккаунтом: **создать нового пользователя**: `new_user_id = uuid4()`, `INSERT users(new_user_id)`, `INSERT auth_identities(user_id=new_user_id, provider='apple', subject=apple_sub, email)` → `target_user_id = new_user_id`.
6. **Upsert привязки устройства** к итоговому пользователю: `auth_devices[resolved_device_id].user_id := target_user_id`.
   - **Конфликт apple_sub-user ≠ device-user** (устройство ранее принадлежало другому `userId`, а apple_sub резолвится в иной аккаунт — кейс «вход в свой Apple-аккаунт на чужом/общем устройстве»): берём **apple_sub-user** как источник истины, устройство **upsert'ится на него** (`UPDATE auth_devices SET user_id = target_user_id, last_seen_at = now() WHERE device_id = ...`). **Авто-merge данных НЕ выполняется** (баланс/история прежнего device-аккаунта не переносятся; прежний аккаунт остаётся доступен по своему refresh-token, если он есть). Зафиксировано осознанно: автоматический merge двух кошельков/историй — продуктовый риск (двойное начисление, конфликт ledger), вынесен в [Q-043-2](../99-open-questions.md).
   - INSERT нового устройства: `INSERT auth_devices(user_id=target_user_id, device_id) ON CONFLICT (device_id) DO UPDATE SET user_id=EXCLUDED.user_id, last_seen_at=now()`.
7. `_issue_pair(target_user_id, resolved_device_id)` — НАША пара токенов (RS256 access + opaque refresh, [ADR-018 §3/§5](ADR-018-embedded-auth-issuer.md)).
8. `commit`.

**Idempotency / гонки:** повторный вход того же `apple_sub` → шаг 5 находит существующую identity → тот же `userId` (никаких дублей; `UNIQUE(provider, subject)` + `ON CONFLICT DO NOTHING` + re-read на конкурентной первой вставке). `auth_devices` upsert — `ON CONFLICT (device_id)`. `email` обновлять при последующих входах **не** требуется (Apple присылает email только при первом согласии; на MVP пишем при создании identity, на резолве не перезаписываем — [Q-043-1](../99-open-questions.md)).

### 6. Безопасность

- **Identity token и nonce не логируются.** `identityToken`/`nonce` покрыты denylist redaction (`src/app/observability/redaction.py`): `*token*` ловит `identityToken`/`identitytoken`; `nonce` уже в `_DENY_EXACT`. Verifier не помещает токен в текст исключений; ошибки — обобщённые (`401` без раскрытия причины). StoreKit/JWT-паттерн (токен никогда в логах) соблюдается.
- **Маппинг ошибок верификации → `401`** (fail-closed): неверная подпись, `iss`/`aud`, `exp`, отсутствие обязательных claims, несовпадение nonce, нерезолвимый JWKS-ключ. Test-mode HS256 вне `APPLE_TEST_MODE` → `401`.
- **Rate-limit:** `enforce_auth_limits(ip)` (per-IP), как все `/v1/auth/*` (анти-абуз перебора токенов).
- **Провайдер-агностично:** фича не зависит от `LLM_PROVIDER` (Anthropic|OpenAI). Работает на всех инстансах одинаково.
- **Аудитория = bundle id** строго (нативный Sign in with Apple). Services ID (web-flow) на MVP не поддерживается — если понадобится web-flow, добавить отдельную допустимую аудиторию ([Q-043-1](../99-open-questions.md)).
- **`APPLE_TEST_SECRET`** — секрет (redaction `*secret*`), используется только при `APPLE_TEST_MODE=true`; в проде test-mode выключен (как `STOREKIT_TEST_MODE`).

## Последствия

- Кросс-девайс аккаунт: один Apple-аккаунт = один `userId` на всех устройствах. Закрывает основную боль [ADR-018](ADR-018-embedded-auth-issuer.md) (потеря аккаунта при смене устройства) для пользователей, прошедших Apple Sign-In.
- Анонимный device-flow ([ADR-018](ADR-018-embedded-auth-issuer.md)) **не ломается**: `register`/`token`/`refresh`/`jwks` без изменений; `auth_devices`/`auth_refresh_tokens` без изменений; `JwtVerifier` без изменений (НАШИ токены те же).
- Связывание сохраняет кредиты/историю при первом Apple-входе с device-аккаунта, у которого ещё нет Apple-идентичности; конфликтные кейсы (чужое устройство / занятый device-аккаунт) разрешаются без авто-merge данных (предсказуемо, без риска двойного начисления).
- Новый внешний trust-anchor — Apple JWKS (`appleid.apple.com`). Сетевая зависимость на верификации (кэш `jwks_cache_ttl_seconds`); недоступность Apple JWKS → `401` для real-токенов до восстановления (fail-closed). Test-mode не зависит от сети.
- Миграция `0012` (single head, expand-only) — добавляется в цепочку `0001`→...→`0011`→`0012`.
- Биллинг неизменен ([ADR-006](ADR-006-credit-billing-and-subscription-grant.md)): Apple Sign-In не начисляет/не списывает кредиты; новый пользователь создаётся с DDL-дефолтами (trial доступен, как у device-register).

## Открытые вопросы

- [Q-043-1](../99-open-questions.md) — ужесточение nonce (обязательный + anti-replay store), обновление `email` при повторных входах, поддержка Services ID (web-flow аудитория), дополнительные провайдеры в `auth_identities`.
- [Q-043-2](../99-open-questions.md) — авто-merge данных (кошелёк/история) при конфликте apple_sub-user ≠ device-user (на MVP merge НЕ выполняется).

## Рассмотренные альтернативы

1. **Apple как внешний issuer (verify-only через `JWT_JWKS_URL`), без выпуска НАШИХ токенов.** Отвергнуто: тогда Apple-токен (TTL ~10 мин, не управляем нами) стал бы access-токеном для всех `/v1/*` — нет refresh-rotation, нет НАШЕГО `device_id`-claim, ломается единый verify-путь и биллинг-идентичность. Решение пользователя: Apple-токен только для входа, далее — НАША пара (как register).
2. **Хранить Apple `sub` прямо в `users` (колонка `apple_sub`).** Отвергнуто: не расширяемо на несколько провайдеров (email/Google), смешивает identity-провайдеры с ядром `users`. Отдельная `auth_identities` чище и расширяема.
3. **Авто-merge кошелька/истории при конфликте.** Отвергнуто на MVP: риск двойного начисления, конфликт append-only ledger ([ADR-005](ADR-005-idempotency-ledger.md)), сложная отмена. Вынесено в [Q-043-2](../99-open-questions.md).
4. **Обязательный nonce.** Отвергнуто на MVP: ломает клиентов без nonce; nonce проверяется при наличии обеих сторон. Ужесточение — [Q-043-1](../99-open-questions.md).
