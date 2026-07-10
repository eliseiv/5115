# Admin — Architecture

## Размещение
- Новый пакет `src/app/admin/` (router + thin service-обёртка над Wallet) и роутер `api_gateway/routers/admin.py`
  под префиксом `/v1/admin`. Структура — в [02-tech-stack.md](../../02-tech-stack.md#структура-проекта-фактическая).
- Admin-роуты подключаются с зависимостью `require_admin` и **без** `get_current_user`.

## Авторизация: `require_admin` (ADR-009)
```
require_admin(x_admin_token: str = Header(...)) -> None  # actor="admin", без userId
```
- Сравнивает `X-Admin-Token` с `ADMIN_API_SECRET` (и опц. `ADMIN_API_SECRET_PREV` на время ротации) — **constant-time**
  (`hmac.compare_digest`). Несовпадение/отсутствие → `401`.
- **НЕ** выполняет lazy-provisioning, **НЕ** читает/трогает `users.trial_used`, **НЕ** создаёт строку `users` для actor.
- Никакого `sub`/пользовательской идентичности: actor фиксируется как `admin` в audit.
- Изоляция: разные секреты/заголовки/зависимости с пользовательским путём → эскалация невозможна by construction (ADR-009 §4).

## Поток grant
```mermaid
sequenceDiagram
    participant OP as Operator
    participant GW as API Gateway (/v1/admin)
    participant ADM as Admin Service
    participant W as Wallet
    participant AU as Audit

    OP->>GW: POST /v1/admin/wallet/grant (X-Admin-Token, {userId, amount, idempotencyKey, reason})
    GW->>GW: require_admin (constant-time compare; rate limit; size limit; validate extra=forbid)
    alt токен невалиден
        GW-->>OP: 401
    else токен валиден
        GW->>ADM: grant(userId, amount, idempotencyKey, reason)
        ADM->>ADM: проверка существования users(userId)
        alt userId не существует
            ADM-->>OP: 404 user_not_found
        else существует
            ADM->>W: WalletService.grant(user_id, amount, idempotency_key, meta{reason}, reason)
            W->>AU: audit billing_credit (idempotent)
            W-->>ADM: {newBalance, ledgerTxId, idempotentReplay}
            ADM->>AU: audit admin_grant (actor=admin, reason, userId, amount, ledgerTxId; БЕЗ секрета)
            ADM-->>OP: 200 {newBalance, ledgerTxId, idempotentReplay}
        end
    end
```

## Поток subscription/grant (ADR-048)
Активация/продление подписки **без** StoreKit-транзакции — для саппорта/компенсации/теста ([ADR-048](../../adr/ADR-048-admin-subscription-grant.md)).
Мотивация: по [ADR-002](../../adr/ADR-002-access-policy-state-machine.md) при `subscription_status=none` кредиты не проверяются (блок `trial_used` при ненулевом балансе) — начисления кредитов мало, нужна активная подписка.
```mermaid
sequenceDiagram
    participant OP as Operator
    participant GW as API Gateway (/v1/admin)
    participant ADM as Admin Service
    participant DB as subscriptions
    participant W as Wallet
    participant AU as Audit

    OP->>GW: POST /v1/admin/subscription/grant (X-Admin-Token, {userId, expiresAt|days, plan?, idempotencyKey, credits?})
    GW->>GW: require_admin (constant-time); admin rate limit; body <= 8 KB; extra=forbid; ровно один из expiresAt/days; expiresAt > now()
    alt токен невалиден
        GW-->>OP: 401
    else токен валиден
        GW->>ADM: grant_subscription(userId, expires_at, plan, idempotencyKey, credits)
        ADM->>ADM: _require_user_exists(userId)
        alt userId не существует
            ADM-->>OP: 404 user_not_found
        else существует
            ADM->>DB: upsert subscriptions (status='active', plan, expires_at) — без StoreKit-verify
            opt эффективные credits > 0 (по умолчанию SUBSCRIPTION_CREDITS_PER_PERIOD)
                ADM->>W: WalletService.grant(user_id, credits, key="admin-sub-grant:{idempotencyKey}", meta{reason})
                W->>AU: audit billing_credit (idempotent)
            end
            ADM->>AU: audit admin_subscription_grant (actor=admin, plan, status, expiresAt, creditsGranted; БЕЗ секрета)
            ADM-->>OP: 200 {status, expiresAt, plan, creditsGranted, newBalance?, ledgerTxId?, idempotentReplay?}
        end
    end
```
- **Upsert напрямую в AdminService** (ORM `Subscription`, `self._session`), **не** через `SubscriptionService` (тот неразрывно связывает upsert с StoreKit-verify — единая ответственность verify→normalize→upsert→grant→audit). Небольшое дублирование трёх присваиваний — сознательный размен ради изоляции verify-less admin-пути; [Q-048-2](../../99-open-questions.md) (не блокер).
- **lazy-expiry учтён:** `expiresAt` требуется строго в будущем — иначе policy-loader (`_effective_subscription_status`) трактовал бы `active` с прошлой датой как `expired`, и грант не снял бы блок.
- **Дефолт `credits`** = `SUBSCRIPTION_CREDITS_PER_PERIOD` (не 0): подписка `active` + баланс 0 = блок `credits_empty`; дефолт-0 не дал бы рабочего доступа «одним запросом». Явный `0` = активировать без начисления.
- Всё в одной транзакции запроса; частичного применения нет.

## Рост admin-surface (ADR-048)
Теперь **две** мутирующие admin-операции (`wallet/grant`, `subscription/grant`) под одним общим `ADMIN_API_SECRET` без scope/least-privilege ([ADR-009](../../adr/ADR-009-admin-token-auth.md) §Consequences). Приемлемо при узком круге операторов; атрибуция/least-privilege — [Q-009-1](../../99-open-questions.md) при дальнейшем росте surface.

## Несуществующий userId — решение
Admin-grant **не создаёт** пользователей (обоснование — [02-api-contracts.md §Обоснование](02-api-contracts.md#обоснование-404-на-несуществующем-userid-не-admin-provisioning)).
Проверка существования `users(userId)` выполняется **до** вызова `WalletService.grant` (который сам делает `_ensure_wallet`,
но не `users`). Отсутствие → `404 user_not_found`. Это сохраняет инвариант ADR-007: единственный путь рождения
идентичности — доверенный issuer.

## Переиспользование Wallet
- `grant`: вызывается **как есть** (`src/app/wallet/service.py:174`); сигнатура `grant(user_id, amount, idempotency_key, meta, reason)`,
  идемпотентна по `(user_id, idempotency_key)`, пишет ledger credit + audit `billing_credit`. `meta` admin-grant включает
  `{"source": "admin", "reason": reason}` (без секретов).
- `get_wallet_view`: для `GET /v1/admin/wallet/{userId}`.
- Admin-модуль **не** дублирует биллинг-логику — только тонкая обёртка (auth + проверка userId + дополнительный audit `admin_grant`).

## Защита
- Отдельный rate limit на `/v1/admin/*` (per source IP, дефолт 10 req/min, env-конфиг). Изолирован от пользовательских лимитов.
- Size-лимит тела admin-grant ≤ 8 KB.
- `X-Admin-Token` добавлен в redaction allowlist (никогда не логируется; ADR-009 §6).
- strict Pydantic v2 (`extra='forbid'`), `amount > 0`, `reason` непустой.
