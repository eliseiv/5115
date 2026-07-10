# ADR-015 — Покупка токенов: consumable StoreKit IAP → grant кредитов

- Статус: Accepted
- Дата: 2026-06-02
- Связан с: ADR-005 (idempotency ledger), ADR-006 (credit billing), subscription, wallet-ledger, модуль `token-purchase`.

## Context
Дизайн «Get More Tokens» предлагает **разовую покупку** пакетов токенов (1500 / 600 / 250 / 100 токенов за деньги), **отдельно** от подписки. Сейчас (ADR-006) кредиты начисляются **только** через subscription grant (фикс. пакет на период, идемпотентно по `transactionId` периода). Публичная докупка пакетов была out of scope (Q-006-1).

StoreKit разделяет типы покупок: подписка (auto-renewable) уже обрабатывается в `subscription/sync`. Покупка пакета токенов — **consumable** IAP: одноразовая транзакция, повторяемая, не возобновляется.

«Токен» в дизайне = единица баланса. В backend единица баланса — **кредит** (`wallets.balance`, BIGINT; ADR-006: 1 кредит = 1 сообщение). Маппинг «токены дизайна → кредиты backend» фиксируется конфигом продуктов (productId → credits), а не клиентом.

## Decision
Ввести **отдельный модуль `token-purchase`** и endpoint обработки consumable-покупки, **не** смешивая с подпиской:

- `POST /v1/tokens/purchase` (JWT) — клиент присылает подписанную StoreKit consumable-транзакцию (тот же verifier, что subscription, включая `STOREKIT_TEST_MODE` для e2e/CI, TD-007).
- Backend **верифицирует** транзакцию (App Store Server API / реальная JWS; fail-closed как в subscription), извлекает `transactionId` и `productId`.
- `productId` маппится в число кредитов через **server-side конфиг** `TOKEN_PRODUCTS` (productId → credits), напр. `tokens_1500 → 1500`. Неизвестный productId → `422`. Маппинг на сервере, не из тела клиента (анти-подделка количества).
- Начисление — `Wallet.grant(credits, idempotency_key=transactionId, type=credit)`. **Идемпотентно по `transactionId`** (тот же unique-index `ux_ledger_idempotency`, ADR-005): повторная отправка той же транзакции не начисляет повторно. `ledger_transactions.meta` помечает `source=token_purchase`, `productId`.
- Возвращает `{ creditsAdded, newBalance }`.

### Доступность покупки (решено — [Q-015-1](../99-open-questions.md) = вариант B, 2026-06-02)
Покупка токенов доступна **только при активной подписке** (`subscription.status == active`). Без активной подписки `POST /v1/tokens/purchase` **отказывает** до начисления — `403` с `code=subscription_required` (см. ниже «Код ответа»). Покупка токенов = **докупка ёмкости сверх месячного пакета** (`SUBSCRIPTION_CREDITS_PER_PERIOD`) для уже подписанных пользователей, а не самостоятельный способ монетизации. Это сохраняет §2 ТЗ («без подписки генерация запрещена») и [ADR-002](ADR-002-access-policy-state-machine.md) **без изменений**: купленные кредиты осмысленны, т.к. их потребитель — подписчик, для которого `credits`-mode уже разблокирован подпиской.

Прежний дефолт Q-015-1 («покупка без подписки разрешена, кредиты — отдельная ось») **отменён**: он создавал «мёртвый» баланс (купить можно, потратить без подписки нельзя) — продуктовое противоречие монетизации MVP. Policy-guard перед grant теперь обязателен (не опционален).

### Код ответа при отсутствии активной подписки
`POST /v1/tokens/purchase` без активной подписки → HTTP **`403`** со стандартным error-телом `{ "code": "subscription_required", "message": "..." }`.

Обоснование выбора `403` (а не `200 + blocked`): правило [ADR-004](ADR-004-blocked-http-200.md) «бизнес-блокировка = `200` + `blockReason`» применяется **только к endpoint'ам генерации/политики** (`/chat/run`, `/chat/tool-result`, `/policy/effective`), где blocked — это успешный бизнес-результат оркестрации. `tokens/purchase` — операция пополнения, не генерация; отказ в выполнении операции — это `403` (консистентно с уже существующим в этом же контракте `userId ≠ sub → 403`). Значение `subscription_required` переиспользуется из enum [ADR-004](ADR-004-blocked-http-200.md), но как `code` в error-формате `4xx`, а не как `blockReason` в `200`. Семантика `subscription_required` (действие требует подписки, её нет) идентична. Это **не** вводит новый код вне enum и **не** нарушает ADR-004.

### Разграничение с subscription grant
- `subscription/sync` → grant фикс. пакета на **период подписки** (idempotency по `transactionId` периода), ADR-006 — **без изменений**.
- `tokens/purchase` → grant по **consumable transactionId**. Разные источники транзакций, разные idempotency-ключи (оба — `transactionId`, но из непересекающихся пространств StoreKit). `meta.source` различает в аудите/истории.
- Инвариант ADR-006 «1 кредит = 1 сообщение» при списании — **без изменений**. Меняется только путь **пополнения** (добавлен второй legitimate источник credit-tx).

## Consequences
- Новый модуль `token-purchase` + endpoint `POST /v1/tokens/purchase`; тонкая обёртка над verifier (общий с subscription) + проверка активной подписки + Wallet.grant.
- **Policy-guard «требует активной подписки»** — обязательная часть пути purchase (проверка `subscription.status == active` **до** `WalletService.grant`). Без подписки — `403 subscription_required`, ledger не пишется, идемпотентность не затрагивается.
- ADR-006 уточняется: credit-tx теперь имеет два источника (subscription period grant; consumable token purchase), оба идемпотентны по соответствующему `transactionId`. Списание не меняется.
- Конфиг `TOKEN_PRODUCTS` (productId→credits) — в 07-deployment env.
- Не требуется новая таблица: переиспользуется `ledger_transactions` (`type=credit`, `meta.source=token_purchase`).

## Alternatives
- **Расширить `subscription/sync` для consumable.** Отвергнуто: смешивает auto-renewable и consumable семантику, усложняет idempotency и refund-логику. Отдельный endpoint чище.
- **Маппинг количества из тела клиента.** Отвергнуто: клиент мог бы подделать число кредитов. Только server-side `TOKEN_PRODUCTS`.
- **Хранить «токены» как отдельную сущность от кредитов.** Отвергнуто: дублирует баланс/ledger; «токен» дизайна = кредит backend по конфигу. Единый баланс проще и согласован с ADR-006.
