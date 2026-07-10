# ADR-047 — Реальный формат payload Adapty: парсер, маппинг событий, идемпотентность гранта по transaction_id

- Статус: Accepted
- Дата: 2026-06-30
- Связано: дополняет и **исправляет** [ADR-029](ADR-029-adapty-subscription-webhook.md) (Adapty subscription webhook) в части §3 (парсинг), §4 (маппинг событий), §5/§6 (идемпотентность гранта); опирается на наблюдаемость [ADR-046](ADR-046-adapty-webhook-outcome-logging.md); адресует маппинг-часть [Q-029-3](../99-open-questions.md); [ADR-005](ADR-005-idempotency-ledger.md) (идемпотентность ledger), [ADR-006](ADR-006-credit-billing-and-subscription-grant.md) (грант кредитов).
- Тип: implementation/bugfix-ADR (исправляет «слепой» парсер ADR-029; контракт `POST /v1/billing/adapty/webhook`, HTTP-семантика, схема БД — **без изменений**; **без миграции**).
- Модуль: [modules/billing-adapty/](../modules/billing-adapty/README.md)

## Контекст

На проде Adapty-вебхуки **игнорируются** (инцидент `broadnova`, [ADR-046](ADR-046-adapty-webhook-outcome-logging.md) сделал слепое пятно видимым). Получены **реальные** payload'ы от Adapty — одна покупка (недельная подписка `week_6.99_nottrial`, 7-дневный free-trial по промо-офферу `ytl`) генерирует **три** события: «Access level updated», «Trial started», «Trial renewal cancelled». Сверка с парсером ADR-029 ([parser.py](../../src/app/billing_adapty/parser.py)) выявила фундаментальные расхождения, из-за которых валидные события молча уходят в `ignored`:

1. **`event_id`.** Парсер ищет `event_id` ‖ `id` → их в payload **нет**, уникальный идентификатор события называется **`profile_event_id`** (у каждого из трёх событий покупки он свой: `a3254174-…`, `815af018-…`, `80d0caf6-…`). Результат — `200 ignored/missing_event_id` на **первой же** проверке, ещё до проверки адресата гранта. Лог ADR-046 показывает неверную причину.
2. **`customer_user_id`.** В payload его **нет** — есть только Adapty `profile_id` (`3bf27b33-…`). Наш `userId` появится в `customer_user_id`, **когда iOS вызовет `Adapty.identify(<наш userId>)`** на стороне клиента (уже в работе у iOS-разработчика). До этого момента корректное поведение — `ignored/missing_customer_user_id` (как сейчас), но теперь **с верной причиной в логах** ([ADR-046](ADR-046-adapty-webhook-outcome-logging.md)).
3. **`event_type`.** В присланном Dashboard-виде нет явного ключа `event_type`; реальные имена событий — `access_level_updated`, `trial_started`, `subscription_renewal_cancelled` / `trial_renewal_cancelled` (плюс стандартные `subscription_started`/`subscription_renewed`/`subscription_expired`/`subscription_cancelled`/`billing_issue_*`). **Точная wire-структура** (где именно лежит `event_type`; плоский payload или обёртка `event_properties`) **на 100% не подтверждена** — типичный Adapty-webhook кладёт бизнес-поля в `event_properties`, а Dashboard-вид показывает их в «расплющенном» виде. Поэтому парсинг проектируется **дефенсивно** (несколько расположений ключа), а финальная сверка wire-формата — по логам [ADR-046](ADR-046-adapty-webhook-outcome-logging.md) после деплоя.
4. **Идемпотентность гранта.** Одна покупка = **несколько** granting-событий с **разными** `profile_event_id`, но **одним** `transaction_id`/`original_transaction_id`. Текущая идемпотентность гранта по `adapty-event:{event_id}` ([ADR-029 §5](ADR-029-adapty-subscription-webhook.md)) дала бы **двойное** (тройное) начисление — по одному ledger-гранту на каждое granting-событие периода.
5. **`subscription_renewal_cancelled` ≠ отзыв доступа.** Отмена автопродления (`will_renew=false`) приходит как отдельное событие, при этом `profile_has_access_level=true` и `subscription_expires_at` в будущем — **доступ сохраняется до конца периода**. ADR-029 знал только `subscription_cancelled` → `expired` и при буквальном маппинге «cancelled→expired» **ошибочно отозвал бы доступ** у пользователя, всего лишь отключившего автопродление.

### Решение пользователя по начислению

Начислять кредиты **сразу на trial**: `trial_started` / `subscription_started` / `subscription_renewed` / `access_level_updated`(`is_active=true`, `access_level_id="premium"`) → грант полного пакета **немедленно**; срок доступа подписки — из `subscription_expires_at`.

## Решение

### A. Парсер реального формата (дефенсивно, с обратной совместимостью)

Все источники проверяются по порядку; **первое непустое строковое значение** выигрывает; вложенный доступ `isinstance`-guarded; отсутствие → `None` (не сбой). `event_properties` (`ep`) — основной носитель бизнес-полей в wire-формате Adapty; плоские (top-level) ключи сохранены как fallback под Dashboard-вид и старые версии payload.

| Поле `ParsedEvent` | Порядок источников (fallback) | Изменение vs ADR-029 |
|---|---|---|
| `event_id` | **`profile_event_id`** ‖ `ep.profile_event_id` ‖ `event_id` ‖ `id` | **NEW: `profile_event_id` первым** (был `event_id`‖`id`) |
| `event_type` | `event_type` ‖ `event` ‖ `ep.event_type` ‖ `type` → **`lower()`** | дефенсивно ещё 3 расположения |
| `customer_user_id` | `customer_user_id` ‖ `profile.customer_user_id` ‖ `ep.customer_user_id` ‖ `user_id` → **UUID** | +`ep.customer_user_id` |
| `vendor_product_id` | `ep.vendor_product_id` ‖ `ep.product_id` ‖ `vendor_product_id` ‖ `product_id` | без изменений |
| `expires_at` | **`ep.subscription_expires_at`** ‖ `ep.expires_at` ‖ `subscription_expires_at` ‖ `expires_at` ‖ `profile.expires_at` (ISO8601→tz-aware; нераспарсиваемое→`None`) | **NEW: `subscription_expires_at` первым** |
| `transaction_id` | `ep.transaction_id` ‖ `transaction_id` | **NEW** |
| `original_transaction_id` | `ep.original_transaction_id` ‖ `original_transaction_id` | **NEW** |
| `is_active` | `ep.is_active` ‖ `is_active` (строго `bool`, иначе `None`) | **NEW** |
| `access_level_id` | `ep.access_level_id` ‖ `access_level_id` | **NEW** |
| `will_renew` | `ep.will_renew` ‖ `will_renew` (строго `bool`, иначе `None`) — **только для audit/лога, в БД НЕ хранится** | **NEW** |

`event_id`, `transaction_id`, `original_transaction_id` могут приходить **числом** (в примере `410003298316682` без кавычек) — парсер должен принять `int` и привести к `str` (для `is_active`/`will_renew` — наоборот, строго `bool`, чтобы `1`/`0` случайно не стали `True`/`False`). Хелпер выбора строкового значения расширяется до приёма `int`→`str(int)`.

### B. Маппинг событий → семантика

`event_type` для `access_level_updated` разрешается **условно** (по `is_active`/`access_level_id`), поэтому диспетчер — функция `classify_event(ParsedEvent) -> Semantics`, а не только membership во frozenset.

| `event_type` | Семантика | Действие над `subscriptions` | Кредиты |
|---|---|---|---|
| `trial_started` | **GRANTING** | `status=active`, `plan=vendor_product_id`, `expires_at` | **грант** (идемпотентно по txn, см. C) |
| `subscription_started` | **GRANTING** | как выше | **грант** |
| `subscription_renewed` | **GRANTING** | как выше | **грант** |
| `access_level_updated`, `is_active=true`, `access_level_id="premium"` | **GRANTING** | как выше | **грант** |
| `subscription_expired` | **EXPIRING** | `status=expired` (plan/expires_at не трогаем) | НЕ трогаем |
| `subscription_cancelled` | **EXPIRING** | `status=expired` | НЕ трогаем |
| `access_level_updated`, `is_active=false` | **EXPIRING** | `status=expired` | НЕ трогаем |
| `subscription_renewal_cancelled` | **NOOP** | **НЕ трогаем** (доступ сохраняется) | НЕ трогаем |
| `trial_renewal_cancelled` | **NOOP** | **НЕ трогаем** | НЕ трогаем |
| `access_level_updated`, `is_active=true`, `access_level_id≠"premium"` ИЛИ `is_active=None` | **NOOP** | **НЕ трогаем** | НЕ трогаем |
| прочее (не в `KNOWN_EVENTS`) | **UNKNOWN** | — | — → `200 ignored` (+эхо `event_type`) |

- **GRANTING** = «доступ появился» → подписка `active`, `expires_at` из payload, начислить кредиты.
- **EXPIRING** = «доступ пропал» → подписка `expired`, кредиты **не трогаем** (consumable-семантика ledger, [ADR-006](ADR-006-credit-billing-and-subscription-grant.md)).
- **NOOP** = «доступ НЕ менялся» (отмена автопродления: `profile_has_access_level=true`, `will_renew=false`) → **доступ НЕ отзывать**, кредиты не менять. Зафиксировано **явно**, чтобы отмена автопродления не блокировала пользователя до конца оплаченного периода. NOOP-событие **всё равно записывается** в `adapty_webhook_events` (дедуп доставки) и в audit (трассировка), но **без** мутации `subscriptions` и без гранта.

`KNOWN_EVENTS = GRANTING_EVENTS ∪ EXPIRING_EVENTS ∪ NOOP_EVENTS ∪ {access_level_updated}`. `access_level_updated` входит в `KNOWN_EVENTS` (проходит gate распознавания), а его итоговая семантика разрешается в `classify_event` по `is_active`/`access_level_id`.

### C. Идемпотентность: дедуп события vs идемпотентность начисления (КРИТИЧНО, разведены)

Два **независимых** механизма:

1. **Дедуп обработки события** — `adapty_webhook_events.event_id` UNIQUE (`event_id` = `profile_event_id`). Защищает от повторной **доставки одного и того же события** Adapty (ретраи). Без изменений по механике ([ADR-029 §6](ADR-029-adapty-subscription-webhook.md)), меняется только источник значения (`profile_event_id`). Каждое из трёх событий покупки имеет **свой** `profile_event_id` → все три проходят дедуп события (это разные события) и записываются.
2. **Идемпотентность начисления** — ledger `idempotency_key = "adapty-txn:{txn}"`, где `txn = transaction_id ‖ original_transaction_id ‖ event_id`. Гарантирует **ровно один грант на период покупки**, сколько бы granting-событий период ни сгенерировал.

**Почему `transaction_id` первичен, а НЕ `original_transaction_id`:** `original_transaction_id` **постоянен на всю цепочку подписки** (одинаков для первичной покупки и **всех** продлений). Если ключом сделать его, **продление** (`subscription_renewed` с новым `transaction_id`, но тем же `original_transaction_id`) схлопнулось бы с первичным грантом → пользователь продлил, но **новых кредитов не получил**. `transaction_id` уникален **на период** (Apple выдаёт новый на каждое продление) → внутри одного периода все события дедуплятся в один грант, а каждый новый период начисляет заново. `original_transaction_id` — лишь fallback (если `transaction_id` отсутствует), `event_id` — крайний fallback (вырожденный случай без любого transaction id; деградирует к старому per-event поведению — приемлемо, т.к. для store-покупок `transaction_id` всегда присутствует).

В примере все три события: `transaction_id=410003298316682` (== `original_transaction_id`) → ключ `adapty-txn:410003298316682` → **один** грант, несмотря на два granting-события (`trial_started` + `access_level_updated`).

**Двойная UNIQUE-граница начисления сохраняется:** `adapty_webhook_events.event_id` UNIQUE (per-событие) + `ledger_transactions (user_id, idempotency_key="adapty-txn:{txn}")` UNIQUE (per-период, [ADR-005](ADR-005-idempotency-ledger.md)).

### D. Тариф product → tokens

Без изменений: `adapty_product_tokens().get(vendor_product_id) or adapty_subscription_tokens_grant` ([ADR-029 §5](ADR-029-adapty-subscription-webhook.md), `config.py`). `week_6.99_nottrial` оператор **может** добавить в `ADAPTY_PRODUCT_TOKENS` (операторский JSON-конфиг, без деплоя кода); иначе fallback `ADAPTY_SUBSCRIPTION_TOKENS_GRANT` (дефолт 1000). Ключ карты должен **точно** совпадать с `vendor_product_id` (включая точки/подчёркивания: `week_6.99_nottrial`). Без изменений `config.py`.

### E. Совместимость, наблюдаемость, миграции

- **Обратная совместимость:** все старые источники полей сохранены как fallback (`event_id`/`id`, `expires_at`, плоские ключи) — payload в старом формате продолжает парситься.
- **HTTP-семантика не меняется:** `200` на всё, кроме провала авторизации (`401`/`500`) и реального сбоя БД (`500` → Adapty ретраит). NOOP/EXPIRING/GRANTING/UNKNOWN — все `200`.
- **ADR-046 не ломается:** структура лога `"adapty_webhook_outcome"` и allowlist полей неизменны. После фикса парсера `event_id` (=`profile_event_id`) реальные payload'ы **перестанут** падать на `missing_event_id` и дойдут до `missing_customer_user_id` (WARNING) — это **точная** причина (iOS ещё не вызвал `Adapty.identify`). **Рекомендуется** (синергия с ADR-046): парсить `event_type` **до** проверки `customer_user_id` (чистая операция, без БД) и передавать его в лог ветки `missing_customer_user_id`, чтобы оператор видел «`trial_started` пришёл, но нет `customer_user_id`», а не безликое `missing_customer_user_id`. Не меняет HTTP-семантику и дедуп.
- **Без миграции.** `will_renew` в БД **не хранится** (парсится только для audit/лога) — отдельная колонка/миграция не вводится (single-head не трогаем). Схема `adapty_webhook_events` неизменна (`event_id text PK` принимает `profile_event_id`). Если в будущем потребуется хранить `will_renew`/`auto_renew_status` — отдельная expand-only миграция со single-head ([Q-047-1](../99-open-questions.md)).
- **Audit** (`adapty_subscription`) расширяется полями `transactionId`, `semantics` (`granting|expiring|noop`), опц. `willRenew` — всё не-секреты, проходят `assert_no_secrets`. Пишется на granting/expiring/noop (внутри транзакции `_apply`, как ADR-029 §7).
- **Не трогаем:** авторизацию (`require_adapty_webhook`), `/v1/subscription/sync`, `/v1/tokens/purchase`, провайдер-абстракцию LLM, контракт `AdaptyWebhookResponse`, биллинг-правило (1 пакет на период).

## Последствия

### Положительные
- Реальные события Adapty **перестают молча игнорироваться**: granting-события начисляют кредиты (как только iOS пришлёт `customer_user_id`).
- **Один грант на период** независимо от числа событий — устранён класс двойного/тройного начисления.
- Отмена автопродления (`*_renewal_cancelled`) больше **не отзывает доступ** — пользователь дорабатывает оплаченный период.
- Дефенсивный парсинг устойчив к неподтверждённой wire-структуре; финальная сверка — по логам ADR-046, без новой выкатки.

### Риски / компромиссы
- **Wire-структура `event_type` не подтверждена на 100%** — парсинг дефенсивный (4 расположения). Если реальный ключ окажется ещё где-то — событие уйдёт в `ignored` (echo) с WARNING ([ADR-046](ADR-046-adapty-webhook-outcome-logging.md)), оператор увидит и доформулирует источник. **Не сбой**, наблюдаемо. Финальная сверка и закрытие [Q-029-3](../99-open-questions.md) — после анализа прод-логов.
- **`customer_user_id` отсутствует до релиза iOS с `Adapty.identify`** — до тех пор все события `ignored/missing_customer_user_id` (WARNING, корректно). Это **клиентская** зависимость, не backend-баг; backend готов принять `customer_user_id`, как только он появится.
- **Грант на trial** означает выдачу полного пакета на 7-дневный trial; при отказе после trial кредиты не возвращаются (consumable-семантика, [ADR-006](ADR-006-credit-billing-and-subscription-grant.md)). Принято решением пользователя (выдавать сразу).
- `transaction_id` как ключ периода предполагает, что Apple выдаёт новый `transaction_id` на каждое продление (стандартное поведение auto-renewable). Если конкретный store/конфиг ведёт себя иначе — наблюдаемо через audit (`transactionId`) и ledger.

## Альтернативы (отклонены)

1. **Идемпотентность гранта по `original_transaction_id`.** Отклонено: постоянен на всю цепочку → продления не начисляли бы кредиты (см. C). Только fallback.
2. **Идемпотентность гранта по `event_id` (как ADR-029).** Отклонено: одна покупка = несколько granting-событий с разными `event_id` → двойное/тройное начисление.
3. **`subscription_renewal_cancelled` → `expired` (буквальный маппинг ADR-029).** Отклонено: ошибочно отзывает доступ при простой отмене автопродления (`profile_has_access_level=true`).
4. **Хранить `will_renew`/состояние автопродления в БД (колонка + миграция).** Отклонено в этой итерации: для корректности доступа/начисления не требуется (NOOP уже не отзывает доступ); добавляет миграцию. Парсим в audit/лог; персист — [Q-047-1](../99-open-questions.md) при потребности.
5. **Pydantic-модель реального payload.** Отклонено (как в ADR-029): `422` на пинг/дрейф → шторм ретраев Adapty. Сохраняем сырое тело + дефенсивный ручной парсинг.
6. **Закрыть [Q-029-3](../99-open-questions.md) этим ADR.** Отклонено: wire-структура не подтверждена на 100%; маппинг **определён**, но закрытие — после верификации по прод-логам ADR-046.
