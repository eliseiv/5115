# ADR-018 — Встроенный auth-issuer в backend (device-based identity, RS256)

- Статус: Accepted
- Дата: 2026-06-02
- Закрывает: [Q-005-1](../99-open-questions.md) (конкретный JWT issuer / flow выпуска токенов)
- Расширен: [ADR-043](ADR-043-sign-in-with-apple.md) (2026-06-19) — Sign in with Apple (`POST /v1/auth/apple`) поверх того же issuer: Apple identity token → НАША пара токенов + кросс-девайс аккаунт через `auth_identities`; закрывает [Q-018-2](../99-open-questions.md). Device-flow ([ADR-018](ADR-018-embedded-auth-issuer.md)) не изменён.
- Связан с: [ADR-001](ADR-001-stack-choice.md) (модульный монолит), [ADR-007](ADR-007-lazy-user-provisioning.md) (lazy provisioning), [ADR-009](ADR-009-admin-token-auth.md) (изоляция admin-auth), [05-security.md](../05-security.md), [03-data-model.md](../03-data-model.md), [modules/auth/](../modules/auth/README.md)

## Context

До сих пор идентичность приходила из **внешнего** доверенного JWT issuer (Apple Sign-In / собственный auth / Firebase), конкретный источник был отложен ([Q-005-1](../99-open-questions.md), must-configure-before-launch). Backend только **верифицировал** RS256-токены существующим `JwtVerifier` (`src/app/api_gateway/auth.py`) по `JWT_JWKS_URL`/`JWT_PUBLIC_KEY` + `JWT_ISSUER`/`JWT_AUDIENCE`. Endpoint регистрации отсутствовал; `users` создавались лениво ([ADR-007](ADR-007-lazy-user-provisioning.md)).

Решение пользователя (2026-06-02): **issuer встраивается в сам backend**. Backend становится одновременно издателем и верификатором токенов. Это снимает зависимость от внешнего IdP для MVP и закрывает [Q-005-1](../99-open-questions.md).

Требуется модель **первичной аутентификации**. Для iOS-MVP анонимная device-based идентичность предпочтительна:
- Бесшовный старт без экранов логина/паролей (пользователь открывает приложение и сразу работает).
- Нет хранения паролей → исчезает целый класс угроз (утечка хэшей, brute-force, reset-flow).
- Совместима с моделью «1 устройство ≈ 1 пользователь» на старте; апгрейд до email/Apple Sign-In не закрывается.

## Decision

**Backend САМ издаёт и верифицирует RS256 JWT. Первичная аутентификация на MVP — device-based.**

### 1. Модель идентичности (device-based, MVP)

- Клиент при первом запуске вызывает `POST /v1/auth/register`, передавая `deviceId` (стабильный идентификатор устройства; на iOS — `identifierForVendor` или Keychain-generated UUID). Если `deviceId` не передан — backend генерирует случайный (UUIDv4) и возвращает его клиенту.
- Backend сопоставляет `(deviceId) → userId`:
  - Если устройство известно (строка в `auth_devices`) — берёт существующий `userId`.
  - Если новое — генерирует `userId = uuid4()`, провижинит `users`-строку (явный provisioning, см. §4), создаёт строку `auth_devices (user_id, device_id)`.
- Выдаёт RS256 JWT с `sub=userId`, `device_id=deviceId`.
- **Анонимность:** PII не собирается. Идентичность = пара ключей устройства, удерживаемая клиентом (см. §3 про повторную аутентификацию).

### 2. Эндпоинты (см. [modules/auth/02-api-contracts.md](../modules/auth/02-api-contracts.md))

- `POST /v1/auth/register` — создать/найти identity по `deviceId`, вернуть access-token (+ refresh-token), `userId`, `deviceId`.
- `POST /v1/auth/token` — повторно получить токены для **уже зарегистрированного** устройства (без создания нового userId, если устройство известно). Идемпотентен по `(deviceId)`.
- `POST /v1/auth/refresh` — обменять валидный refresh-token на новую пару (rotation). Refresh-token включён в MVP (см. §5).
- `GET /v1/auth/jwks` (опционально-публичный) — JWKS с публичным ключом для самопроверки/отладки/будущих верификаторов; не содержит приватного ключа.

Все `auth`-эндпоинты — **без** пользовательского JWT (это точка его получения). Защита — rate-limit (§6).

### 3. Подпись и верификация (self-consistent)

- Backend подписывает **приватным RS256-ключом**. Алгоритм — `RS256`, в заголовке `kid` (для будущей ротации ключей и JWKS).
- Верификация — **существующим** `JwtVerifier` (`src/app/api_gateway/auth.py`), без изменения его логики верификации: тот же `RS256`, проверка `exp/iss/aud/sub`. Issuer/audience — **собственные**:
  - `iss = JWT_ISSUER` (значение: `https://broadnova.shop`, совпадает с [Q-017-1](../99-open-questions.md) `SERVICE_DOMAIN`).
  - `aud = JWT_AUDIENCE` (значение: `claude-ios`).
- Self-consistent loop: тот же сервис, что подписал, верифицирует выпущенный токен своим публичным ключом. `JWT_JWKS_URL` для self-issued режима **не используется** (verifier берёт `JWT_PUBLIC_KEY`); встроенный issuer и verifier разделяют одну ключевую пару из config.

### 4. Согласование с lazy-provisioning ([ADR-007](ADR-007-lazy-user-provisioning.md))

- **`register` создаёт `users`-строку явно** (eager provisioning): `INSERT INTO users (id) VALUES (:new_user_id) ON CONFLICT (id) DO NOTHING`. Тот же идемпотентный upsert, что и в gateway — никакого нового механизма.
- **Lazy-provisioning остаётся как fallback** (ADR-007 не отменяется): если по какой-то причине `users`-строки нет к моменту первого `/v1/*` запроса (например, токен пережил очистку или будущий внешний issuer), `get_current_user` всё равно создаёт её. Два пути идемпотентны и не конфликтуют (`ON CONFLICT DO NOTHING`).
- **`trial_used` / policy не затронуты:** `register` создаёт `users` строго с DDL-дефолтами (`trial_used=FALSE`), как и lazy-путь. Биллинг ([ADR-002](ADR-002-access-policy-state-machine.md)/[ADR-006](ADR-006-credit-billing-and-subscription-grant.md)) видит нового пользователя ровно так же, как видел при внешнем issuer.

### 5. Refresh-token (включён в MVP)

- **Access-token TTL = 1 час** (`AUTH_ACCESS_TTL_SECONDS`, дефолт 3600). Короткий, чтобы ограничить окно при утечке.
- **Refresh-token включён** (а не Q-отложен): без него клиенту пришлось бы повторно слать `deviceId` на каждое истечение, что эквивалентно долгоживущему секрету устройства. Refresh-token — **opaque** (случайная высокоэнтропийная строка, не JWT), хранится в таблице `auth_refresh_tokens` как `token_hash` (SHA-256), TTL = 30 дней (`AUTH_REFRESH_TTL_SECONDS`, дефолт 2592000).
- **Rotation:** `POST /v1/auth/refresh` инвалидирует предъявленный refresh-token (single-use) и выдаёт новый. Reuse уже использованного → `401` + ревокация всей цепочки устройства (детект кражи). Хранение хэша, не plaintext.
- Альтернатива «refresh = JWT» отвергнута: opaque-токен позволяет серверную ревокацию (logout/кража), JWT без stateful-стора отозвать нельзя.

### 6. Безопасность

- **Rate-limit на `/v1/auth/*`** (анти-abuse массовой регистрации): дефолт `AUTH_RATE_LIMIT_PER_IP=10 req/min per IP` (создание identity — дорогая операция). Использует существующий per-IP лимитер gateway.
- **Защита от массовой генерации identity** — [Q-018-1](../99-open-questions.md) (дефолт: per-IP rate-limit + опциональный App Attest / device-check как post-MVP усиление; на MVP rate-limit достаточен).
- `deviceId` валидируется: строка, `1..128` символов, charset `[A-Za-z0-9._:-]` (`extra='forbid'`, schema-уровень).
- **Приватный ключ никогда не логируется** — `JWT_PRIVATE_KEY`/`JWT_PRIVATE_KEY_PATH` в redaction allowlist (поля `*key*`/`*secret*` уже покрыты, добавляется явный allowlist). Подписанный JWT и refresh-token также не логируются.
- Приватный ключ — **секрет**, не в репозитории/образе (см. §7).

### 7. Управление ключами и PEM-в-env

Многострочный PEM плохо переносится через `.env`. Решение — **поддержать оба механизма, приоритет у файла-пути:**

- `JWT_PRIVATE_KEY_PATH` / `JWT_PUBLIC_KEY_PATH` — путь к PEM-файлу (рекомендуемый prod-способ: mount секрета файлом, без экранирования). Если задан — читается из файла.
- `JWT_PRIVATE_KEY` / `JWT_PUBLIC_KEY` (уже есть `JWT_PUBLIC_KEY`) — PEM-строка в env с **поддержкой `\n`-экранирования**: литералы `\n` в значении заменяются на реальные переводы строк при загрузке config. Это позволяет положить однострочное значение в `.env`/secret manager.
- **Приоритет:** `*_PATH` > строковое значение. Если ни путь, ни строка не заданы для приватного ключа — issuer-эндпоинты возвращают `503` (auth не сконфигурирован), но verify-only режим (внешний issuer) продолжает работать на `JWT_PUBLIC_KEY`/`JWT_JWKS_URL`.
- Публичный ключ — для verify (не секрет). Приватный — секрет (env / secret manager / mounted-файл, `.gitignore`, redaction).

## Consequences

**Положительные:**
- Закрыт [Q-005-1](../99-open-questions.md): issuer определён (встроенный, device-based), prod-блокер снят.
- Бесшовный onboarding iOS без логина/паролей; нет хранения паролей.
- Reuse существующего `JwtVerifier` — verify-путь не меняется, весь `/v1/*` контур работает как раньше.
- Серверная ревокация через refresh-token store (logout/кража устройства).

**Отрицательные / ограничения:**
- Идентичность привязана к устройству: при потере устройства/переустановке без сохранённого refresh-token пользователь получит **новый** `userId` (потеря истории/баланса). Решается апгрейдом до email/Apple Sign-In ([Q-018-2](../99-open-questions.md)) — путь не закрыт.
- Анонимная регистрация уязвима к Sybil/abuse — митигируется rate-limit; усиление (App Attest) — [Q-018-1](../99-open-questions.md).
- Backend теперь хранит приватный ключ подписи — расширяет поверхность секретов (митигируется §6/§7).

**Тестирование:**
- `register` нового устройства создаёт `users` + `auth_devices` + выдаёт верифицируемый собственным verifier токен (round-trip sign→verify).
- `register` известного устройства возвращает тот же `userId` (идемпотентность по `deviceId`).
- `refresh` rotation: старый refresh инвалидируется, reuse → `401` + ревокация.
- Совместимость с lazy-provisioning: токен с `sub`, которого нет в `users`, всё равно провижинит на первом `/v1/*` (ADR-007 fallback).

## Alternatives

1. **Внешний IdP (Apple Sign-In / Firebase) на MVP.** Отвергнуто (решение пользователя): добавляет внешнюю зависимость и интеграционную работу; device-based проще для анонимного MVP. Путь к Apple Sign-In оставлен открытым ([Q-018-2](../99-open-questions.md)).
2. **Email/пароль как первичный flow.** Отвергнуто для MVP: требует хранения паролей (хэши, reset, верификация email), экранов логина; противоречит «бесшовный анонимный старт». Спроектирован как опциональное расширение ([Q-018-2](../99-open-questions.md)), путь не закрыт.
3. **Только access-token без refresh (re-`register` на каждое истечение).** Отвергнуто: либо длинный TTL (большое окно при утечке), либо частая пересылка `deviceId` (фактически долгоживущий секрет без возможности ревокации). Refresh-token с rotation строго лучше.
4. **Refresh-token как JWT.** Отвергнуто: невозможна серверная ревокация без stateful-стора; opaque + hashed-store в БД даёт logout/детект кражи.
5. **`HS256` (симметричная подпись).** Отвергнуто: контур уже стандартизирован на `RS256` ([05-security.md](../05-security.md)); асимметрия позволяет раздать публичный ключ (JWKS) будущим верификаторам без раскрытия секрета подписи.
