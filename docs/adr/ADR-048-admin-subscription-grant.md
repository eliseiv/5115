# ADR-048 — Admin-активация подписки: `POST /v1/admin/subscription/grant`

- Статус: Accepted
- Дата: 2026-07-01
- Связан с: [ADR-009](ADR-009-admin-token-auth.md) (admin-auth), [ADR-002](ADR-002-access-policy-state-machine.md) (policy state machine), [ADR-006](ADR-006-credit-billing-and-subscription-grant.md) (биллинг/грант кредитов), [ADR-007](ADR-007-lazy-user-provisioning.md) (lazy provisioning), [05-security.md](../05-security.md), [modules/admin/](../modules/admin/README.md)

## Context

Admin-API ([ADR-009](ADR-009-admin-token-auth.md), `src/app/api_gateway/routers/admin.py`) на сегодня умеет **только** начислять кредиты
(`POST /v1/admin/wallet/grant`) и смотреть кошелёк (`GET /v1/admin/wallet/{userId}`). Эндпоинта активации подписки **нет**.

Проблема саппорта/компенсации/тестирования. По [ADR-002](ADR-002-access-policy-state-machine.md) в режиме `credits` при `subscription_status = none`
кредиты **не проверяются** — пользователь блокируется по `trial_used` (после израсходованного одного пожизненного триала), **даже с ненулевым балансом**
(`src/app/policy/engine.py:102-111`). То есть **начисления кредитов недостаточно** — чтобы дать рабочий доступ, оператору нужно уметь **активировать подписку**.

Штатный путь активации — `POST /v1/subscription/sync` (StoreKit-верификация, `src/app/subscription/service.py`) и Adapty-вебхук ([ADR-029](ADR-029-adapty-subscription-webhook.md)) —
**требуют подписанной транзакции** от Apple/Adapty. У оператора её нет (компенсация, ручная выдача, тест на реальном устройстве без покупки). Нужен **admin-путь без StoreKit-транзакции**.

Дополнительная тонкость (learнед из policy-loader): `src/app/policy/loader.py::_effective_subscription_status` применяет **lazy expiry** — строка `status='active'` с `expires_at <= now()`
трактуется как `expired`. Значит admin-грант с датой в прошлом **не** даст доступа. А «подписка active + баланс 0» → блок `credits_empty`. Оба факта формируют контракт ниже.

## Decision

Новый эндпоинт **`POST /v1/admin/subscription/grant`** под той же изолированной admin-схемой ([ADR-009](ADR-009-admin-token-auth.md)): заголовок `X-Admin-Token` (`require_admin`),
admin body-size cap (≤ 8 KB), admin rate-limit (дефолт 10 req/min per source IP), тег OpenAPI `Admin`, strict Pydantic (`extra='forbid'`). Прямой upsert строки `subscriptions` **без** StoreKit-верификации,
с **опциональным** начислением кредитов в том же запросе и в **одной транзакции**.

### 1. Тело запроса (`AdminSubscriptionGrantRequest`, StrictModel)

| Поле | Тип | Обяз. | Правила |
|---|---|---|---|
| `userId` | UUID | да | Существующий пользователь. Отсутствует → `404 user_not_found` (admin не создаёт пользователей, [ADR-007](ADR-007-lazy-user-provisioning.md)). |
| `expiresAt` | ISO8601 datetime (tz-aware) | ровно одно из `expiresAt`/`days` | Момент истечения подписки. **Должен быть tz-aware и строго в будущем** (`> now()`), иначе `422`. |
| `days` | int `> 0` | ровно одно из `expiresAt`/`days` | Срок в днях от `now()`; сервер вычисляет `expires_at = now() + days`. `≤ 0` → `422`. |
| `plan` | str (≤ 128) | нет | Метка плана. Дефолт `"manual_grant"`. |
| `idempotencyKey` | str (1..128) | да | Ключ идемпотентности начисления кредитов (см. §3). |
| `credits` | int `≥ 0` | нет | Сколько кредитов начислить вместе с активацией. **Опущено (null) → `SUBSCRIPTION_CREDITS_PER_PERIOD`** (см. обоснование). Явный `0` → активировать подписку **без** начисления. `< 0` → `422`. |

**Форма срока — «ровно один из `expiresAt`/`days`» (обоснование).** Поддержаны оба, но задан должен быть строго один (валидатор `model_validator`; оба заданы → `422`, ни одного → `422`).
`expiresAt` даёт оператору точную дату (совпадает с тем, как `expires_at` кладёт StoreKit-путь), `days` — эргономичный shortcut «дай N дней». Взаимоисключение убирает неоднозначность «что победит».
Вариант «только expiresAt» отвергнут: оператору саппорта чаще нужен именно «плюс N дней», без ручного вычисления даты. Вариант «только days» отвергнут: нельзя выставить конкретную дату окончания под тариф.
**Требование «строго в будущем»** обязательно из-за lazy expiry (см. Context): грант в прошлое молча не дал бы доступа. Неограниченная (null-expiry) подписка **вне scope** — оператор может задать далёкую дату; см. [Q-048-1](../99-open-questions.md).

### 2. Дефолт `credits` = `SUBSCRIPTION_CREDITS_PER_PERIOD` при опущенном поле (обоснование выбора)

Ключевая цель эндпоинта — **одним запросом дать рабочий доступ**. Подписка `active` + баланс `0` = блок `credits_empty` ([ADR-002](ADR-002-access-policy-state-machine.md)). Если бы дефолт был `0`,
оператор, забывший передать `credits`, активировал бы подписку, а пользователь всё равно оставался бы заблокирован (`credits_empty`) — то есть эндпоинт по умолчанию **не решал бы** исходную проблему.
Поэтому дефолт = `SUBSCRIPTION_CREDITS_PER_PERIOD` (тот же фиксированный пакет, что начисляет реальный период подписки, `src/app/subscription/service.py:71-83`) — активированная вручную подписка ведёт себя как настоящий период.
Оператор при необходимости переопределяет: `credits: 0` — активировать без начисления (например, у пользователя уже есть баланс), `credits: N` — конкретное число. Новый config **не вводится** — переиспользуется существующий `SUBSCRIPTION_CREDITS_PER_PERIOD`.

### 3. Поведение

1. `require_admin` (constant-time compare) + admin rate-limit + admin body-size cap ≤ 8 KB + strict-валидация тела.
2. Проверка существования `users(userId)` **до** любых записей — переиспользуется `AdminService._require_user_exists` (`404 user_not_found` при отсутствии; admin никогда не создаёт users, [ADR-007](ADR-007-lazy-user-provisioning.md)).
3. **Upsert `subscriptions`** (по PK `user_id`): `status='active'`, `plan = <plan>`, `expires_at = expiresAt | now()+days`. Прямая запись через ORM-модель `Subscription`, **без** StoreKit-верификации (в отличие от `SubscriptionService.sync`). Upsert естественно идемпотентен по `user_id` (PK): повтор перезаписывает те же значения.
4. **Опциональное начисление кредитов.** Эффективная сумма = `credits` если задан, иначе `SUBSCRIPTION_CREDITS_PER_PERIOD`. Если сумма `> 0` → `WalletService.grant(...)` **как есть** (`src/app/wallet/service.py:174`) — атомарно, идемпотентно по `(user_id, idempotency_key)`, пишет ledger `credit` + audit `billing_credit`. Идемпотентный ledger-ключ **производный, с namespace**: `f"admin-sub-grant:{idempotencyKey}"` — чтобы человекочитаемый `idempotencyKey` не коллидировал с ключами `admin/wallet/grant` (raw) и реальных периодов (`sub-grant:{transaction_id}`). Если сумма `== 0` — начисление не выполняется.
5. **Audit.** Пишется новое событие `admin_subscription_grant` (actor=`admin`, `userId`, `plan`, `status`, `expiresAt`, `creditsGranted`, `idempotencyKey`, `ledgerTxId` при наличии) — симметрично `admin_grant`; **секрет `X-Admin-Token` в audit/логи не пишется**. При начислении дополнительно есть штатный `billing_credit` (из `WalletService.grant`).
6. **Одна транзакция.** Upsert + grant + оба audit-события — в рамках сессии запроса (общий `AsyncSession`, коммит один раз, как в существующих admin/subscription путях). Частичного применения нет.

### 4. Ответ (`AdminSubscriptionGrantResponse`, 200)

| Поле | Тип | Прим. |
|---|---|---|
| `status` | str | Новый статус подписки (`"active"`). |
| `expiresAt` | ISO8601 \| null | Эффективный момент истечения (из `expiresAt` или `now()+days`). |
| `plan` | str \| null | Записанный план. |
| `creditsGranted` | int | Эффективно начисленная сумма (0, если не начислялось). |
| `newBalance` | int \| null | Баланс после начисления; `null`, если `creditsGranted == 0`. |
| `ledgerTxId` | uuid \| null | Id credit-транзакции; `null`, если начисления не было. |
| `idempotentReplay` | bool \| null | `true`, если грант был повтором того же payload; `null`, если начисления не было. |

Credit-поля (`newBalance`/`ledgerTxId`/`idempotentReplay`) присутствуют **только** когда `creditsGranted > 0` (иначе ledger-транзакции не существует — возвращать её id/баланс было бы вводящим в заблуждение; лишнего чтения баланса не делаем).

### 5. Коды ответа

`200`; `401` (нет/неверный `X-Admin-Token`); `404` (`user_not_found`); `409` (тот же `idempotencyKey` с **другим** `credits` — из `WalletService.grant`, начисления/активации нет); `422` (нет `userId`; оба/ни одного из `expiresAt`/`days`; `expiresAt` не tz-aware / в прошлом; `days ≤ 0`; `credits < 0`; схема/`extra=forbid`); `429` (admin rate-limit); `5xx`.

### 6. Переиспользование и границы

- `WalletService.grant` — **как есть** (не дублируем биллинг).
- Новый метод `AdminService.grant_subscription(...)` — upsert `subscriptions` через ORM `Subscription` **непосредственно в AdminService** (у сервиса уже есть `self._session`), **без** захода в `SubscriptionService`. Обоснование: `SubscriptionService.sync` неразрывно связывает upsert с StoreKit-верификацией (единая ответственность verify→normalize→upsert→grant→audit); тянуть в него verify-less admin-путь усложнило бы класс. Небольшое дублирование трёх присваиваний (`status`/`plan`/`expires_at`) — сознательный размен ради изоляции путей; см. [Q-048-2](../99-open-questions.md) (не блокер).
- Существующие флоу `admin/wallet/*`, `/v1/subscription/sync`, Adapty-вебхук, BYOK — **не меняются**.

### 7. Без миграций

Таблица `subscriptions` существует ([03-data-model](../03-data-model.md), `src/app/models/tables.py:69`). Новое событие `admin_subscription_grant` — строковое значение `event_type` (колонка `Text`, без enum-ограничения) → миграция не нужна.

## Consequences

**Положительные:**
- Оператор активирует/продлевает подписку из Swagger **одним** запросом, включая рабочий баланс кредитов — исходная проблема (`trial_used`-блок при ненулевом балансе) решена по умолчанию.
- Согласованность с policy-инвариантами: требование «expiresAt строго в будущем» гарантирует, что грант реально снимает блок (lazy expiry учтён).
- Полная изоляция от пользовательского JWT и от StoreKit — admin-путь не подделывает транзакцию, а делает явный аудируемый upsert.
- Реиспользование `WalletService.grant` и `_require_user_exists` — без дублирования биллинга и без второго пути рождения идентичности.

**Отрицательные / ограничения:**
- Admin-surface растёт: теперь **две** мутирующие admin-операции (`wallet/grant`, `subscription/grant`) под одним общим секретом без scope/least-privilege (см. [ADR-009](ADR-009-admin-token-auth.md) §Consequences). Приемлемо при узком круге операторов; least-privilege/атрибуция — [Q-009-1](../99-open-questions.md) при дальнейшем росте surface.
- Ручной грант не связан со StoreKit/Adapty — при последующей реальной покупке источником истины остаётся Adapty ([ADR-029](ADR-029-adapty-subscription-webhook.md)); admin-грант может быть перезаписан вебхуком (это ожидаемо, upsert по `user_id`).
- Нет «бессрочной» подписки (null-expiry) — сознательно, чтобы исключить случайные вечные гранты; далёкая дата — обходной путь ([Q-048-1](../99-open-questions.md)).

## Alternatives

1. **Расширить `POST /v1/subscription/sync` admin-режимом (bypass verify по admin-токену).** Отвергнуто: смешивает пользовательский JWT-эндпоинт с admin-авторизацией, ломает изоляцию контуров ([ADR-009](ADR-009-admin-token-auth.md) §4) и единую ответственность `SubscriptionService` (verify — обязателен).
2. **Только начисление кредитов (без активации подписки).** Отвергнуто как недостаточное: не снимает блок `trial_used` при `subscription_status=none` ([ADR-002](ADR-002-access-policy-state-machine.md)) — исходная проблема.
3. **Отдельный микро-сервис/скрипт активации в обход API.** Отвергнуто: обходит аудит/rate-limit/валидацию admin-контура, нет единого источника истины и наблюдаемости.
4. **Дефолт `credits = 0`.** Отвергнуто (см. §2): по умолчанию оставлял бы пользователя заблокированным `credits_empty`, не решая задачу «одним запросом рабочий доступ».
