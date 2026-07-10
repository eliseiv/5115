# ADR-029: Adapty subscription webhook — основной путь биллинга по подпискам

- Статус: Accepted
- Дата: 2026-06-12
- Связано: [ADR-006](ADR-006-credit-billing-and-subscription-grant.md) (грант кредитов на период подписки), [ADR-005](ADR-005-idempotency-ledger.md) (идемпотентность ledger), [ADR-009](ADR-009-admin-token-auth.md) (образец статической bearer-авторизации, constant-time), [ADR-015](ADR-015-consumable-token-iap.md) (consumable token IAP — НЕ через Adapty), [ADR-017](ADR-017-shared-server-traefik-deploy.md) (per-instance секреты), [ADR-002](ADR-002-access-policy-state-machine.md) (состояние подписки в Policy).
- Модуль: [modules/billing-adapty/](../modules/billing-adapty/README.md)

## Контекст

iOS-команда перешла на [Adapty](https://adapty.io) как платформу управления подписками. После события покупки/продления/отмены/истечения подписки Adapty шлёт серверный HTTP POST webhook на наш backend. По событию мы должны обновить подписку пользователя (`subscriptions`) и начислить кредиты (токены) по тиру продукта — идемпотентно.

Adapty становится **основным путём биллинга по подпискам**. Существующие пути сохраняются без изменений:

- `POST /v1/subscription/sync` (StoreKit JWS, модуль [subscription](../modules/subscription/README.md)) — **оставляем как есть**, не ломаем. Но источник истины по статусу подписки теперь — Adapty-вебхук.
- `POST /v1/tokens/purchase` (consumable StoreKit IAP, [ADR-015](ADR-015-consumable-token-iap.md), модуль [token-purchase](../modules/token-purchase/README.md)) — **оставляем как есть**. Consumable-пакеты токенов в этой итерации **НЕ** идут через Adapty.

### Ограничения, диктуемые платформой Adapty (из ТЗ, согласованы)

1. **Adapty НЕ подписывает payload** — нет HMAC-подписи тела. Единственный механизм аутентификации — статический bearer-токен `Authorization: Bearer <secret>`, который оператор задаёт в Adapty UI при настройке вебхука. Это симметричный shared secret, аналог нашего admin-токена.
2. **Проверочный пинг при сохранении.** При сохранении вебхука Adapty шлёт проверочный запрос с пустым / не-JSON / неполным телом и **не сохранит** конфигурацию вебхука, пока не получит `2xx`. Значит, эндпоинт обязан отвечать `2xx` на «мусорное» тело **после успешной авторизации**.
3. **Бесконечный ретрай не-2xx.** Adapty ретраит любой ответ ≠ 2xx **бесконечно**. Следовательно: на любой корректно авторизованный, но «кривой» / нераспознанный payload отвечаем `2xx` со статусом `ignored` (анти-абуз ретраев). `5xx` отдаём **только** при реальном внутреннем сбое (например, БД недоступна) — тогда ретрай Adapty желателен и приведёт к чистой переобработке.

## Решение

Вводим эндпоинт **`POST /v1/billing/adapty/webhook`** (наша `/v1`-конвенция) и новый модуль `billing-adapty`.

### 1. Авторизация (per-route bearer, изолированный секрет)

- Заголовок `Authorization: Bearer <ADAPTY_WEBHOOK_SECRET>`. Сравнение — constant-time `hmac.compare_digest` по образцу `_admin_token_matches` / `require_admin` (`src/app/api_gateway/auth.py:99-134`).
- Неверный / отсутствующий токен → **401** (без раскрытия причины).
- Секрет **не задан** в env (`ADAPTY_WEBHOOK_SECRET=""`) → **500** с понятным текстом мис-конфигурации. Пустой секрет **никогда** не матчится (как admin: blank header не аутентифицирует).
- **Изоляция секрета:** `ADAPTY_WEBHOOK_SECRET` отдельный от пользовательского JWT, admin-секрета (`ADMIN_API_SECRET`), KMS, preview-секрета. Per-instance (мульти-инстанс, [ADR-017](ADR-017-shared-server-traefik-deploy.md)) — у каждого инстанса свой секрет.
- **Не глобальный middleware**, а per-route dependency (глобального auth-middleware нет, `main.py:196-212`). Эндпоинт исключён из пользовательской JWT-цепочки.

### 2. Тело запроса — всегда 2xx после авторизации

Читаем **сырое** тело `await request.body()` **БЕЗ Pydantic-валидации тела** (Pydantic дал бы `422` на проверочный пинг → Adapty не сохранит вебхук и/или зациклит ретраи). Дефенсивный JSON-парсинг вручную. Матрица ответов (всё HTTP `200` кроме отмеченного):

| Состояние | HTTP | `result` | `reason` |
|---|---|---|---|
| нет/неверный bearer | 401 | — | (UnauthorizedError) |
| `ADAPTY_WEBHOOK_SECRET` не задан | 500 | — | misconfiguration (понятный текст) |
| пустое тело | 200 | `ignored` | `empty_body` |
| не-JSON | 200 | `ignored` | `invalid_json` |
| JSON не объект (массив/скаляр) | 200 | `ignored` | `not_an_object` |
| нет `event_id` | 200 | `ignored` | `missing_event_id` |
| нет `customer_user_id` | 200 | `ignored` | `missing_customer_user_id` |
| пользователь не найден | 200 | `ignored` | `user_not_found` |
| неизвестный `event_type` | 200 | `ignored` | (+ эхо `event_type`) |
| дубликат `event_id` | 200 | `duplicate` | — |
| валидное распознанное событие | 200 | `applied` | — |
| реальный внутренний сбой (БД и т. п.) | **500** | — | (→ Adapty ретраит) |

### 3. Дефенсивный парсинг (поля кочуют между версиями Adapty SDK/payload)

- `event_id` = `event_id` ‖ `id`
- `event_type` → привести к `lower()`
- `customer_user_id` = `customer_user_id` ‖ `profile.customer_user_id` ‖ `user_id` — **это наш `userId` (UUID)**, проставленный iOS-клиентом в Adapty как customerUserId.
- `vendor_product_id` = `event_properties.vendor_product_id` ‖ `event_properties.product_id` ‖ `vendor_product_id` ‖ `product_id`
- `expires_at` (опц.) = `event_properties.expires_at` ‖ `profile.expires_at` (ISO8601 → tz-aware datetime; нераспарсиваемое → трактуем как отсутствующее, событие всё равно `applied`)

`customer_user_id`, не парсящийся как UUID, эквивалентен «пользователь не найден» → `200 ignored/user_not_found`.

### 4. Четыре типа событий

| `event_type` | действие над `subscriptions` | кредиты |
|---|---|---|
| `subscription_started` | `status=active`, `plan=vendor_product_id`, `expires_at` (если есть) | **начислить** по тиру (идемпотентно) |
| `subscription_renewed` | `status=active`, `plan`, `expires_at` | **начислить** по тиру (идемпотентно) |
| `subscription_cancelled` | `status=expired` | **НЕ трогаем** |
| `subscription_expired` | `status=expired` | **НЕ трогаем** |

`cancelled`/`expired` означают «доступ прекращён» — Policy ([ADR-002](ADR-002-access-policy-state-machine.md)) увидит `expired`. Кредиты не возвращаются и не списываются (consumable-семантика ledger, [ADR-006](ADR-006-credit-billing-and-subscription-grant.md)).

### 5. Тир product → tokens (config-хелпер)

JSON-карта env `ADAPTY_PRODUCT_TOKENS` (`{vendor_product_id: tokens}`) по образцу `Settings.token_products()` (`config.py:199`). Если для `vendor_product_id` нет записи в карте — fallback на фиксированный `ADAPTY_SUBSCRIPTION_TOKENS_GRANT` (целое > 0; дефолт 1000). Парсер: только строковые ключи и положительные int-значения (исключая bool); малформед → пустая карта → используется fallback. Имена env финализированы в [07-deployment.md](../07-deployment.md) и [02-tech-stack.md](../02-tech-stack.md).

> Почему отдельный `ADAPTY_SUBSCRIPTION_TOKENS_GRANT`, а не reuse `SUBSCRIPTION_CREDITS_PER_PERIOD`: изоляция конфигурации Adapty-пути от StoreKit-`sync`-пути, чтобы операторы могли калибровать гранты независимо и чтобы ретирование StoreKit-`sync` ([Q-029-2](../99-open-questions.md)) не затрагивало Adapty. Дефолты совпадают (1000) для предсказуемости.

### 6. Идемпотентность — одна транзакция

Новая таблица `adapty_webhook_events` (`event_id` UNIQUE, `user_id`, `event_type`, `payload` JSONB, `processed_at`). Алгоритм обработки распознанного события — в **одной БД-транзакции**:

1. `INSERT INTO adapty_webhook_events (event_id, ...) ... ON CONFLICT (event_id) DO NOTHING RETURNING event_id`.
2. Если конфликт (ничего не вставлено) → `200 duplicate` (раннее завершение, **ничего не мутируем: нет upsert, нет grant, нет audit**).
3. Иначе (`applied`):
   - upsert `subscriptions` (по образцу `subscription/service.py:52-68`);
   - для `started`/`renewed`: `WalletService.grant(user_id=..., amount=<тир>, idempotency_key="adapty-event:{event_id}", reason="adapty_subscription", meta={...})` (`wallet/service.py:174-236`);
   - **запись audit `adapty_subscription`** (см. §7) — пишется **только здесь, на `applied`**;
   - запись события (тот же INSERT шага 1) фиксируется.
4. Commit. **Любой сбой → откат всей транзакции → 500 → Adapty ретраит → чистая переобработка** (на ретрае `event_id` снова свободен, т. к. INSERT откатился; двойного начисления нет — `grant` идемпотентен по `idempotency_key`, а `event_id`-INSERT — единая точка дедупликации).

**Двойная защита от двойного начисления:** (1) `adapty_webhook_events.event_id` UNIQUE; (2) `ledger_transactions (user_id, idempotency_key="adapty-event:{event_id}")` UNIQUE ([ADR-005](ADR-005-idempotency-ledger.md)).

### 7. Audit

Новое событие `EVENT_ADAPTY_SUBSCRIPTION = "adapty_subscription"` (`src/app/audit/service.py`). Payload `{adaptyEventId, eventType, status, plan, expiresAt, customerId}` — проходит через `assert_no_secrets` (`audit/service.py:48`). Bearer-секрет в payload не попадает; заголовок `Authorization` уже покрыт redaction-денилистом (`authorization` ∈ `_DENY_SUBSTRINGS`, `redaction.py:15`).

**Audit пишется ТОЛЬКО на исходе `applied`** (внутри транзакции §6, после успешного дедуп-INSERT и upsert/grant). На `ignored` (любой `reason`, включая `user_not_found` и неизвестный `event_type`) и на `duplicate` audit **НЕ пишется** — это «событие не применено», мутаций нет. Так аудит не засоряется проверочными пингами Adapty и повторными доставками.

## Последствия

### Положительные
- Единый основной путь биллинга подписок (Adapty), управляемый платформой.
- Сильная идемпотентность (две независимые UNIQUE-границы), безопасный ретрай Adapty.
- Устойчивость к расхождению версий payload Adapty (дефенсивный парсинг).
- Секрет изолирован, constant-time, per-instance, redacted.

### Риски / компромиссы
- **Двойное начисление между путями.** Adapty-вебхук и StoreKit-`sync` используют **разные** idempotency-ключи (`adapty-event:{event_id}` vs `sub-grant:{transaction_id}`). Если клиент задействует **оба** пути на одну покупку — кредиты начислятся дважды. **Митигация (контракт):** клиент использует **ОДИН** путь. iOS на Adapty-сборке шлёт только Adapty-события и **не** вызывает `/v1/subscription/sync` для подписок. Зафиксировано в [05-security.md](../05-security.md) и [01-context.md](../modules/billing-adapty/01-context.md). Полное ретирование `sync` — [Q-029-2](../99-open-questions.md) (отложено), [TD-021](../100-known-tech-debt.md).
- **Нет криптоподписи payload.** Аутентичность = знание bearer-секрета. Утечка секрета = возможность форжить события. Митигация: высокоэнтропийный секрет, TLS на edge, per-instance ротация, audit. Принято как ограничение платформы.
- **Реальные имена `event_type` Adapty** требуют сверки с продакшн-конфигом Adapty ([Q-029-3](../99-open-questions.md)). Парсинг устойчив: неизвестный тип → `ignored`, не сбой.

## Альтернативы (отклонены)

- **Pydantic-валидация тела** — даёт `422` на проверочный пинг и любой дрейф payload → Adapty не сохранит вебхук / зациклит ретраи. Отклонено в пользу сырого тела + ручного парсинга.
- **HMAC-проверка подписи** — Adapty её не предоставляет. Невозможно.
- **Возврат 4xx на кривой payload** — Adapty ретраит не-2xx бесконечно → шторм ретраев. Отклонено; кривой payload → `200 ignored`.
- **Consumable-пакеты через Adapty в этой итерации** — отклонено, остаётся `/v1/tokens/purchase` ([ADR-015](ADR-015-consumable-token-iap.md)). Перенос — [Q-029-1](../99-open-questions.md), [TD-020](../100-known-tech-debt.md).
- **Reuse `EVENT_SUBSCRIPTION_CHANGE`** для audit — допустимо, но отдельное `adapty_subscription` точнее трассирует источник. Выбрано отдельное событие.
