# Auth — Overview

## Назначение
Первичная аутентификация и **выпуск** пользовательских JWT собственным backend'ом (встроенный issuer, [ADR-018](../../adr/ADR-018-embedded-auth-issuer.md)). Backend становится издателем и верификатором токенов одновременно — внешний IdP на MVP не нужен. Закрывает [Q-005-1](../../99-open-questions.md).

## In-scope (MVP)
- Device-based анонимная идентичность: `deviceId` → `userId` (find-or-create).
- Выпуск RS256 access-token (`sub`, `device_id`, `iss`, `aud`, `exp`, `iat`, `kid`).
- Opaque refresh-token с rotation (single-use, hashed-store, серверная ревокация).
- Эндпоинты: `register`, `token`, `refresh`, `jwks`, **`apple`** ([ADR-043](../../adr/ADR-043-sign-in-with-apple.md)).
- **Sign in with Apple:** верификация Apple identity token (OIDC RS256) → выпуск НАШЕЙ пары токенов; кросс-девайс аккаунт через `auth_identities`. Закрывает [Q-018-2](../../99-open-questions.md).
- Явный provisioning `users` при `register`/Apple-входе; согласование с lazy-provisioning ([ADR-007](../../adr/ADR-007-lazy-user-provisioning.md)).
- Управление ключевой парой RSA (приватный — секрет; PEM-в-env / файл-путь).
- Rate-limit и anti-abuse регистрации.

## Out-of-scope (MVP)
- **Email/пароль** как первичный flow — опциональное расширение, не MVP (остаток [Q-018-2](../../99-open-questions.md)). Путь не закрыт (можно добавить `users.email`/`password_hash` без слома device-based).
- **Services ID / web-flow Apple, доп. провайдеры (Google и т.п.)** — post-MVP ([Q-043-1](../../99-open-questions.md)); на MVP — нативный Sign in with Apple (aud=bundle id) через `POST /v1/auth/apple`.
- **App Attest / DeviceCheck** усиление анти-Sybil — post-MVP ([Q-018-1](../../99-open-questions.md)); на MVP — per-IP rate-limit.
- Логин-экраны, сессии-cookie, password reset, email-верификация.
- **Авто-merge данных** (кошелёк/история) при конфликте идентичностей — не MVP ([Q-043-2](../../99-open-questions.md)).
- Перенос идентичности между устройствами (account recovery) для **анонимных** device-аккаунтов без Apple-входа — остаётся ограничением device-based.

## Ключевые инварианты
- `users.id ≡ JWT sub` (UUID) — без изменений относительно [ADR-007](../../adr/ADR-007-lazy-user-provisioning.md).
- Один `userId` на `deviceId` (find-or-create по `auth_devices`).
- Приватный ключ подписи — секрет, никогда не логируется, не в репозитории/образе.
- Верификация выпущенных токенов — тем же `JwtVerifier`, что и прежде (RS256, собственные `iss`/`aud`).
