# Auth — Data Model

Таблицы `auth_devices`/`auth_refresh_tokens` — **миграция `0005`** (expand-only). Таблица `auth_identities` (Sign in with Apple) — **миграция `0012`** ([ADR-043](../../adr/ADR-043-sign-in-with-apple.md)). Сводный DDL — [03-data-model.md](../../03-data-model.md) (таблицы 18–19, 21). `users` **не меняется** (идентичность по-прежнему `users.id ≡ sub`, [ADR-007](../../adr/ADR-007-lazy-user-provisioning.md)).

## 18. auth_devices
```sql
CREATE TABLE auth_devices (
    device_id   TEXT PRIMARY KEY,                                   -- стабильный id устройства (клиент или сгенерированный backend)
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_auth_devices_user ON auth_devices (user_id);
```
> Маппинг `deviceId → userId` (find-or-create, [03-architecture.md](03-architecture.md)). `device_id` — PK (одно устройство = одна идентичность). `UNIQUE` обеспечивается PK; гонка одновременной регистрации разрешается `ON CONFLICT (device_id) DO NOTHING` + повторное чтение.

## 19. auth_refresh_tokens
```sql
CREATE TABLE auth_refresh_tokens (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    device_id   TEXT NOT NULL REFERENCES auth_devices(device_id) ON DELETE CASCADE,
    token_hash  TEXT NOT NULL,                       -- sha256(opaque refresh token), НЕ plaintext
    expires_at  TIMESTAMPTZ NOT NULL,
    used_at     TIMESTAMPTZ,                          -- single-use: проставляется при rotation
    revoked_at  TIMESTAMPTZ,                          -- ревокация цепочки при reuse-детекте/logout
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ux_refresh_token_hash ON auth_refresh_tokens (token_hash);
CREATE INDEX ix_refresh_user_device ON auth_refresh_tokens (user_id, device_id);
```
> Opaque refresh-token хранится **только** как хэш. `used_at`/`revoked_at` реализуют single-use rotation и анти-кражу: предъявление токена с непустым `used_at` → reuse → ревокация цепочки (`SET revoked_at=now WHERE user_id=? AND device_id=?`). Истёкшие/использованные/отозванные строки — кандидаты на фоновую очистку ([TD-013](../../100-known-tech-debt.md), не блокер MVP).

## 21. auth_identities ([ADR-043](../../adr/ADR-043-sign-in-with-apple.md), миграция `0012`)
```sql
CREATE TABLE auth_identities (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider    TEXT NOT NULL,                       -- 'apple' (расширяемо: email/google/...)
    subject     TEXT NOT NULL,                       -- провайдерский стабильный id (apple sub)
    email       TEXT,                                -- опционально (может быть private-relay)
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ux_auth_identities_provider_subject ON auth_identities (provider, subject);
CREATE INDEX ix_auth_identities_user ON auth_identities (user_id);
```
> Внешние identity-провайдеры (Sign in with Apple на старте). `UNIQUE(provider, subject)` — точка кросс-девайс резолва (один Apple-аккаунт = один `userId`) и гонко-безопасности (`ON CONFLICT (provider, subject) DO NOTHING` + повторное чтение, как `auth_devices`). `ix_auth_identities_user` — обратный lookup «есть ли у `userId` Apple-идентичность» (связывание, [03-architecture.md](03-architecture.md#sign-in-with-apple-adr-043)). Миграция `0012` (expand-only, `down_revision=0011_workspaces`, single head). `users`/`auth_devices`/`auth_refresh_tokens` НЕ меняются.

## Инварианты
- `auth_devices.user_id`, `auth_refresh_tokens.user_id` и `auth_identities.user_id` всегда указывают на существующую `users`-строку (provisioning при `register`/Apple-входе, FK `ON DELETE CASCADE`).
- `auth_identities`: ровно одна строка на `(provider, subject)` (UNIQUE); у одного `userId` ≤ 1 Apple-идентичности в норме (инвариант связывания [ADR-043 §5](../../adr/ADR-043-sign-in-with-apple.md) — повторный device-аккаунт с Apple-идентичностью триггерит создание нового пользователя, а не вторую apple-строку).
- `users.id ≡ sub` сохраняется: `register` задаёт `userId` явно, как и lazy-path.
- Refresh-token валиден ⟺ `used_at IS NULL AND revoked_at IS NULL AND expires_at > now()`.
- Один активный (не used/revoked) refresh-token на устройство в норме — после rotation предыдущий помечен `used_at`.
