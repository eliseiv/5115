# ADR-053 — Резолв пользователя во входящем CloudPayments-вебхуке через `auth_devices` (deviceId → userId)

- Статус: Accepted
- Дата: 2026-07-03
- Тип: bugfix-ADR, **исправляет [ADR-050 §2/§5](ADR-050-cloudpayments-webhook.md)** (резолв пользователя входящего вебхука). Расширяет RU-контур [ADR-050](ADR-050-cloudpayments-webhook.md) (вход) / [ADR-051](ADR-051-cloudpayments-checkout-payment-link.md) (checkout) / [ADR-052](ADR-052-cloudpayments-webhook-lenient-auth-header.md) (auth-заголовок).
- Связано: [ADR-018](ADR-018-embedded-auth-issuer.md) (device-based identity, таблица `auth_devices`), [ADR-007](ADR-007-lazy-user-provisioning.md) (не провижинить пользователей из вебхука), [ADR-005](ADR-005-idempotency-ledger.md) (идемпотентность гранта). Модуль [billing-cloudpayments](../modules/billing-cloudpayments/README.md).

## Context

**Инцидент (прод avelyra, подтверждён БД + документом iOS-разработчика `RU_PURCHASE_FLOW.md`): «оплата есть — начисления нет» для RU-флоу.**

В приложении у одного пользователя **два разных идентификатора**:

| Идентификатор | Пример | Где используется |
|---|---|---|
| **deviceId** | `55cbe083-fcbd-4460-af62-06f9a7bea97c` | broadapps: клиент создаёт платёж `POST pay.broadapps.dev/api/v1/payments/link` с `user_id = deviceId`. Этот же `deviceId` затем возвращается в колбэке как `AccountId`/`Data.user_id`. |
| **JWT userId** | `b0f407bd-4a19-449e-beab-84ce341d6915` | наш backend: кредиты, policy, чат, subscription, wallet, ledger. |

Связь **deviceId → userId** хранится у нас в таблице `auth_devices(device_id PK, user_id FK→users)` ([ADR-018](ADR-018-embedded-auth-issuer.md), device-based identity): строка создаётся при `POST /v1/auth/register` (до оплаты). Проверено в БД прода:
```
auth_devices[55cbe083-...] = b0f407bd-...     # deviceId → userId
users[55cbe083-...]        = ОТСУТСТВУЕТ       # 55cbe083 — это deviceId, НЕ userId
users[b0f407bd-...]        = существует        # реальный пользователь
```

**Корень бага.** CloudPayments-вебхук ([ADR-050 §2/§5](ADR-050-cloudpayments-webhook.md), `src/app/billing_cloudpayments/service.py`) резолвит пользователя из `AccountId`→`Data.user_id`, нормализует к lower/UUID и ищет его **только в `users`** (`_user_exists`). Т. к. broadapps присылает **deviceId**, а deviceId в `users` отсутствует → lookup даёт `ignored/user_not_found` → грант/подписка **не применяются**. Реальный пользователь (`b0f407bd`) остаётся без кредитов и подписки, хотя платёж прошёл. Это корневая причина класса «оплата есть — начисления нет» на RU-пути.

**Почему это не всплыло раньше.** Инцидент маскировался предыдущими фиксами того же контура: [ADR-051](ADR-051-cloudpayments-checkout-payment-link.md) (checkout берёт `userId` из JWT `sub`) и [ADR-052](ADR-052-cloudpayments-webhook-lenient-auth-header.md) (терпимый разбор auth-заголовка, снявший `401`). После прохождения авторизации колбэк доходит до сервиса, но спотыкается уже на резолве пользователя — потому что клиент (iOS) на RU-пути шлёт broadapps **deviceId**, а не JWT `sub` (расхождение с намерением [ADR-051](ADR-051-cloudpayments-checkout-payment-link.md), где `user_id` = JWT `sub`).

**Архитектурный контекст sync-механизма RU-оплаты (операторская заметка, НЕ наш код).** Факт успешной RU-оплаты доходит до нас **серверным колбэком broadapps → наш вебхук** `POST /v1/billing/cloudpayments/webhook`. Это единственный доверенный канал начисления. Клиентский вызов `POST /v1/billing/cloudpayments/checkout` ([ADR-051](ADR-051-cloudpayments-checkout-payment-link.md)) как «синхронизация оплаты» **избыточен и вреден**: повторный checkout создаёт **дубль-платёж** (новую платёжную ссылку/попытку), а не подтверждает уже прошедшую оплату. Это пункт iOS-стороны (не менять поведение checkout-эндпоинта в этом ADR) — фиксируется для ясности контура.

## Decision

Ввести **двухступенчатый резолв `userId`** во входящем CloudPayments-вебхуке: полученный из колбэка идентификатор трактуется сначала как наш `userId`, при промахе — как `deviceId` с резолвом в `userId` через **нашу** таблицу `auth_devices`. Всё начисление/подписка/дедуп/идемпотентность — на **резолвнутый** `userId`. Скоуп — **только** CloudPayments-вебхук; Adapty, checkout, миграции, anti-tamper и идемпотентность **не трогаются**.

### 1. Двухступенчатый резолв `userId` (`service.py`)

Парсинг идентификатора из тела **не меняется** ([ADR-050 §2](ADR-050-cloudpayments-webhook.md)): `X ← _first_str(AccountId, Data.user_id).lower()` → `uuid.UUID(X)` (нет/не-UUID → `ignored/invalid_account_id`). Далее — резолв `X` в наш `userId`:

- **(a)** `X` есть в `users` (`SELECT 1 FROM users WHERE id = X`) → `X` — уже **наш `userId`**, использовать напрямую. `resolvedVia = "user_id"`. Обеспечивает обратную совместимость: если broadapps когда-нибудь пришлёт настоящий `userId` (намерение [ADR-051](ADR-051-cloudpayments-checkout-payment-link.md)), поведение прежнее.
- **(b)** иначе `X` есть в `auth_devices.device_id` (`SELECT user_id FROM auth_devices WHERE device_id = X`) → взять связанный `user_id` (**deviceId → userId**) и использовать его для начисления/подписки. `resolvedVia = "device_id"`. Это и есть фикс реального RU-флоу (broadapps шлёт deviceId).
- **(c)** иначе (`X` нет ни в `users`, ни в `auth_devices`) → `ignored/user_not_found` (как сейчас: `200 {"code":0}` + **WARNING**, без создания пользователя/устройства).

Резолв заменяет прежний одноступенчатый `_user_exists(X)` (Stage 3 в `handle()`). Реализация — новый метод резолва (например `_resolve_user(x: uuid.UUID) -> tuple[uuid.UUID, str] | None`, возвращает `(resolved_user_id, resolved_via)` или `None`); `parser` **не трогается** (резолв — это DB-логика сервиса, не чистый парсинг).

**Ключ поиска в `auth_devices`.** `auth_devices.device_id` — `TEXT PRIMARY KEY` ([03-data-model.md §18](../03-data-model.md)), значения deviceId — UUID-строки; iOS-документ подтверждает нижний регистр (`55cbe083-...`). Ищем по нормализованной-lower строковой форме `X` (`str(X)` от уже-lower UUID). Регистр deviceId в `auth_devices` при создании (`/v1/auth/register`) — как прислал клиент; на RU-пути тот же клиент шлёт тот же lower deviceId в broadapps, поэтому lower-сопоставление совпадает. (Если появится расхождение регистра — покрывается наблюдаемостью §3: `user_not_found` при заведомо оплатившем.)

### 2. Всё начисление — на резолвнутый `userId`

После §1 в `ParsedPayment.user_id` кладётся **резолвнутый** `userId` (наш внутренний id из `users`), **не** исходный `X`/deviceId. Всё дальнейшее ([ADR-050 §3/§4](ADR-050-cloudpayments-webhook.md)) работает на нём **без изменений**:

- upsert `subscriptions ON CONFLICT (user_id)` — на резолвнутый `userId`;
- грант кредитов `WalletService.grant(user_id=<resolved>, ...)`;
- дедуп события `INSERT cloudpayments_webhook_events (user_id=<resolved>, transaction_id, ...)`;
- идемпотентность гранта `idempotency_key = cp-txn:{TransactionId}` (**не** меняется — ключ по `TransactionId`, не по userId; namespace изолирован);
- anti-tamper (сумма только из серверных карт), audit `cloudpayments_payment` — на резолвнутый `userId`.

Ledger / subscription / wallet ключуются **нашим** `userId`. deviceId в бизнес-ключи начисления **не попадает** (только как вход резолва и как поле лога, §3).

### 3. Наблюдаемость

В структурный лог `"cloudpayments_webhook_outcome"` ([ADR-050 §7](ADR-050-cloudpayments-webhook.md), [08-observability.md](../modules/billing-cloudpayments/08-observability.md)) добавляется поле **`resolvedVia`** ∈ `"user_id"` | `"device_id"` (на исходах, где пользователь резолвнут: `applied`/`duplicate`/`unknown_product`; на `user_not_found` — резолв не удался, `resolvedVia` опущено). `userId` в логе — **резолвнутый** наш внутренний UUID (безопасно логировать). Дополнительно допустимо логировать `accountId` (= исходный `X`/deviceId) — тоже наш внутренний id, безопасно, помогает диагностике deviceId→userId; PII-карт и секреты по-прежнему **запрещены** ([ADR-050 §7](ADR-050-cloudpayments-webhook.md)). Цель: оператор видит, что RU-платежи резолвятся `resolvedVia="device_id"` (ожидаемо), и отличает их от прямого `userId`.

### 4. Безопасность и консистентность

- **Маппинг — только из нашей БД.** deviceId→userId берётся **исключительно** из `auth_devices` (наша таблица, заполняется доверенным `/v1/auth/register`). Телу колбэка для маппинга **не доверяем** — из тела берём только сам идентификатор `X`, а связь `X→userId` устанавливает наша БД. Клиент не может подставить чужой `userId`: он контролирует лишь `X`, а `auth_devices[X]` жёстко привязан к тому `userId`, за которым устройство закреплено на регистрации.
- **Без провижининга.** `X`, которого нет **ни** в `users`, **ни** в `auth_devices`, → `user_not_found` (§1c). **Не** создаём пользователей/устройства из вебхука ([ADR-007](ADR-007-lazy-user-provisioning.md): единственный источник идентичности — auth-issuer).
- **Гонки.** `auth_devices` — стабильная строка, создаётся при `/v1/auth/register` **до** оплаты (устройство должно зарегистрироваться, чтобы получить JWT и уйти на оплату). Оба lookup (`users`, `auth_devices`) — в **той же** транзакции обработки вебхука; на момент колбэка строка `auth_devices` уже существует. Гонки «оплата раньше регистрации» практически нет; если бы произошла — деградирует в `user_not_found` (безопасно, ретраибельно контрактно, оплатившему грант не теряется навсегда — повторный колбэк/сверка).
- **Приоритет `users` над `auth_devices`.** Порядок (a)→(b) детерминирован: если `X` случайно есть и в `users`, и в `auth_devices.device_id` (теоретически, т. к. оба UUID-подобны, но пространства раздельны), выигрывает трактовка «это наш userId». Коллизия крайне маловероятна (device_id — клиентский UUID устройства, id пользователя — серверный `gen_random_uuid()`), но порядок фиксирует однозначность.

### 5. Скоуп и что НЕ трогается

- **Только CloudPayments-вебхук** (`src/app/billing_cloudpayments/service.py`): broadapps использует deviceId.
- **Adapty-вебхук НЕ трогать** — там своя семантика `customer_user_id` (= наш `userId` через `Adapty.identify`, [ADR-047](ADR-047-adapty-real-payload-format-and-grant-idempotency.md)); двухступенчатый резолв к Adapty **не применяется**.
- **Checkout-эндпоинт ([ADR-051](ADR-051-cloudpayments-checkout-payment-link.md)) НЕ трогать** — он уже берёт `userId` из JWT `sub`; поведение и контракт не меняются.
- **Без миграций** — `auth_devices` уже существует ([ADR-018](ADR-018-embedded-auth-issuer.md), миграция `0005`); новых колонок/таблиц нет.
- **anti-tamper, идемпотентность (`cp-txn:{TransactionId}`), дедуп события, парсер, auth-заголовок ([ADR-052](ADR-052-cloudpayments-webhook-lenient-auth-header.md)), HTTP-семантика (`{"code":0}`/401/500)** — без изменений.

### 6. Обратная совместимость

Если `X` уже `userId` (есть в `users`) — путь (a), поведение **строго прежнее** ([ADR-050 §5](ADR-050-cloudpayments-webhook.md)). Новый путь (b) активируется только когда `X` не найден в `users`, но найден в `auth_devices` — раньше это давало `user_not_found`, теперь корректно начисляет. Изменение — **чистое расширение** резолва (ни один ранее успешный исход не меняется; часть ранее ошибочных `user_not_found` становится `applied`).

## Consequences

**Плюсы:**
- Закрыт корневой инцидент «оплата есть — начисления нет» RU-флоу: deviceId из broadapps резолвится в наш `userId` через `auth_devices`, кредиты/подписка начисляются реальному пользователю.
- Обратная совместимость полная: прямой `userId` (намерение [ADR-051](ADR-051-cloudpayments-checkout-payment-link.md)) продолжает работать; никакой ранее успешный платёж не ломается.
- Маппинг — из доверенной нашей БД, не из тела; клиент не может подменить адресата гранта.
- Без миграций, без изменения контракта/HTTP-семантики, без риска для Adapty/StoreKit/checkout.
- Наблюдаемость `resolvedVia` даёт оператору явную картину пути резолва.

**Минусы / риски / долг:**
- Один доп. DB-lookup (`auth_devices`) на путь `user_not_found`-в-`users` (индексирован по PK/`device_id`; ничтожно).
- Расхождение регистра deviceId между `/v1/auth/register` и broadapps (если появится) → `user_not_found`; митигация — lower-нормализация обеих сторон + наблюдаемость (`user_not_found` при заведомо оплатившем виден в WARNING).
- Не закрывает [Q-052-1](../99-open-questions.md) (точный формат auth-заголовка broadapps) — ортогонально, остаётся открытым.
- Операторская заметка (iOS): клиентский `/checkout` как «sync» избыточен (дубль-платёж) — вне нашего кода, фиксируется контекстом; поведение checkout не меняем.

## Alternatives (отвергнуто)

- **Провижинить `users` из deviceId в вебхуке.** Отвергнуто: нарушает [ADR-007](ADR-007-lazy-user-provisioning.md) (источник идентичности — auth-issuer); создаст «пользователя-призрака» без JWT-истории, оторванного от реального аккаунта, которому и принадлежит устройство.
- **Доверять маппингу из тела колбзка (клиент шлёт и deviceId, и userId).** Отвергнуто: клиент-контролируемый маппинг = подмена адресата гранта. Связь deviceId→userId должна приходить из **нашей** `auth_devices`, а не из тела.
- **Чинить только на клиенте (iOS шлёт broadapps JWT `sub` вместо deviceId).** Отвергнуто как единственная мера: (1) требует релиза iOS и оставляет уже-оплативших без начисления; (2) сервер обязан быть устойчив к обоим идентификаторам. Серверный двухступенчатый резолв закрывает инцидент немедленно и остаётся корректным после любого поведения клиента (путь (a) поймает прямой `userId`).
- **Расширить резолв на Adapty-вебхук симметрично.** Отвергнуто: Adapty шлёт `customer_user_id` = наш `userId` (через `Adapty.identify`), там deviceId не фигурирует; лишняя логика = риск регресса основного пути. Скоуп строго CloudPayments.
