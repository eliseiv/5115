# billing-cloudpayments / 08 — Observability (логирование исхода вебхука)

Базово — [ADR-050 §7](../../adr/ADR-050-cloudpayments-webhook.md); **АКТУАЛЬНО — [ADR-054](../../adr/ADR-054-cloudpayments-webhook-payment-verification.md)** (auth-лог переименован; в outcome добавлены поля верификации/реконсиляции). Образец — [billing-adapty/08-observability.md](../billing-adapty/08-observability.md) ([ADR-046](../../adr/ADR-046-adapty-webhook-outcome-logging.md)). HTTP-семантику (`{"code":0}`/429/500), начисление, дедуп и контракт лог **не меняет**.

Два независимых лога:
- `"cloudpayments_webhook_auth_observed"` ([ADR-054](../../adr/ADR-054-cloudpayments-webhook-payment-verification.md)) — **наблюдательный** (не привязан к 401, которого больше нет), эмитится в `require_cloudpayments_webhook` (`auth.py`), см. §Auth-observed ниже.
- `"cloudpayments_webhook_outcome"` — эмитится в `CloudPaymentsWebhookService.handle()` (`service.py`), см. ниже.

## Auth-observed лог ([ADR-054](../../adr/ADR-054-cloudpayments-webhook-payment-verification.md); ранее auth-denied [ADR-052](../../adr/ADR-052-cloudpayments-webhook-lenient-auth-header.md))
**Изменение [ADR-054](../../adr/ADR-054-cloudpayments-webhook-payment-verification.md):** эндпоинт публичный, `401` **снят** → прежний WARNING `"cloudpayments_webhook_auth_denied"` (на 401) **переименован** в `"cloudpayments_webhook_auth_observed"` (уровень **DEBUG/INFO**), эмитится наблюдательно (авторизация не блокирует). Цель прежняя — видимость, если broadapps позже введёт подпись/иной заголовок ([Q-052-1](../../99-open-questions.md) закрыт: сейчас `authScheme=none`).

**Файл:** `src/app/billing_cloudpayments/auth.py` (логгер `app.billing_cloudpayments.auth`, `log_event`).

**Allowlist полей (без изменений):**
| Поле | Значение |
|---|---|
| `matched` | `bool` — совпал ли (опц.) присутствующий токен с легаси `CLOUDPAYMENTS_WEBHOOK_TOKEN` (для лога; НЕ гейтит) |
| `authScheme` | слово-схема в lower (`bearer`/`token`/…) при «схема+значение», иначе `none` (нет заголовка; **ожидаемо для broadapps**) / `empty` / `raw` (значение НЕ логируется) |
| `presentAuthHeaders` | список **имён** присутствующих заголовков из allowlist `("authorization","x-api-key","x-signature","x-sign","x-webhook-signature","x-content-hmac","content-hmac","signature")` |

**ЗАПРЕЩЕНО:** значение токена/секрета, полный заголовок `Authorization`, значения любых заголовков, сырое тело.

**Ориентиры для qa:** колбэк broadapps без `Authorization` → `authScheme="none"`, `matched=false`, **пропускается** (нет 401); один DEBUG/INFO `auth_observed`-лог; в записи нет значений/секрета.

## Outcome лог (после авторизации, [ADR-050 §7](../../adr/ADR-050-cloudpayments-webhook.md))

## Цель
Каждый вызов `CloudPaymentsWebhookService.handle()` пишет **ровно одну** структурную запись `"cloudpayments_webhook_outcome"`, чтобы исход (в т.ч. `user_not_found`/`unknown_product`) был виден оператору. Тело HTTP-ответа несёт только `{"code":0}` — причина исхода живёт **в логе**, не в ответе.

## Файл для правки (outcome-лог)
Только `src/app/billing_cloudpayments/service.py` (auth-denied лог — отдельно, в `auth.py`, §выше). **Роутер НЕ трогать** (он всегда возвращает `{"code":0}` и не видит `product_id`/`user_id`).

## Импорты и логгер
```python
import logging
from app.observability.logging import log_event
logger = logging.getLogger(__name__)   # == "app.billing_cloudpayments.service"
```

## Helper эмиссии
```python
def _log_outcome(
    self,
    outcome: WebhookOutcome,
    *,
    transaction_id: str | None = None,
    product_id: str | None = None,
    user_id: uuid.UUID | None = None,
    kind: str | None = None,
    resolved_via: str | None = None,   # ADR-053: "user_id" | "device_id" | None
) -> WebhookOutcome:
    level = _level_for(outcome.result, outcome.reason)
    log_event(
        logger, level, "cloudpayments_webhook_outcome",
        result=outcome.result, reason=outcome.reason,
        transactionId=transaction_id, productId=product_id,
        userId=str(user_id) if user_id is not None else None, kind=kind,
        resolvedVia=resolved_via,
    )
    return outcome
```
- `user_id: uuid.UUID` → **обязательно** `str(...)` (иначе `json.dumps` падает). `None`-поля `JsonFormatter` выкидывает из JSON (отсутствие = «не распарсено»).
- **`resolvedVia`** ([ADR-053](../../adr/ADR-053-cloudpayments-webhook-user-resolution-via-auth-devices.md)) — как резолвнут пользователь: `"user_id"` (`X` найден в `users`) \| `"device_id"` (`X` найден в `auth_devices.device_id`, deviceId→userId). Присутствует на исходах, где пользователь резолвнут (`applied`/`duplicate`/`unknown_product`); на `user_not_found` резолв не удался → опущено. `userId` в логе — **резолвнутый наш внутренний UUID** (безопасно). deviceId (= исходный `X`) — тоже наш внутренний id, безопасно логировать (опционально как `accountId`); карт-PII/секреты по-прежнему запрещены.

## Функция уровня
```python
def _level_for(result: str, reason: str | None) -> int:
    if result in ("applied", "duplicate"):
        return logging.INFO
    # result == "ignored"
    if reason in ("user_not_found", "unknown_product"):
        return logging.WARNING
    if reason == "empty_body":
        return logging.DEBUG
    # invalid_json | not_an_object | not_a_completed_payment | missing_transaction_id
    # | invalid_data | missing_product_id | invalid_account_id
    return logging.INFO
```

| `result` | `reason` | Level |
|---|---|---|
| `applied` | `None` | INFO |
| `duplicate` | `None` | INFO |
| `ignored` | `user_not_found` | **WARNING** |
| `ignored` | `unknown_product` | **WARNING** |
| `ignored` | `empty_body` | DEBUG |
| `ignored` | прочие (`invalid_json`/`not_an_object`/`not_a_completed_payment`/`missing_transaction_id`/`invalid_data`/`missing_product_id`/`invalid_account_id`) | INFO |

## Точки вызова (каждый `return` → ровно один лог)
В `handle()` каждый ранний `return _ignored(...)` оборачивается в `_log_outcome(...)` с тем контекстом, что **уже распарсен** на этой точке (`transaction_id`/`product_id`/`user_id`/`kind` — по мере доступности; иначе `None`). Финальная `return await self._apply(parsed)` **остаётся как есть** — `_apply` логирует свои `duplicate`/`applied` сам (иначе двойная запись).

| Точка | Исход | известные поля |
|---|---|---|
| `not raw` | `ignored/empty_body` | — |
| `json.loads` упал / не dict | `ignored/invalid_json` \| `not_an_object` | — |
| нет `TransactionId` | `ignored/missing_transaction_id` | — |
| гейт не прошёл | `ignored/not_a_completed_payment` | `transaction_id` |
| `Data` не парсится | `ignored/invalid_data` | `transaction_id` |
| нет `product_id` | `ignored/missing_product_id` | `transaction_id` |
| невалидный `AccountId` | `ignored/invalid_account_id` | `transaction_id`, `product_id` |
| пользователь не найден | `ignored/user_not_found` | `transaction_id`, `product_id` (`user_id` НЕ резолвнут → опущено; `resolvedVia` опущено) |
| `unknown` / token не в карте | `ignored/unknown_product` | `transaction_id`, `product_id`, `user_id` (резолвнутый), `kind`, `resolvedVia` |
| `_apply` → `duplicate`/`applied` | — | логирует `_apply` (`transaction_id`/`product_id`/`user_id` (резолвнутый)/`kind`/`resolvedVia`) |

> На `user_not_found` в логе **резолвнутого `userId` нет** (резолв не удался: `X` не в `users` и не в `auth_devices`); поле `userId` опущено (не `null`-ключ). Ранее (до [ADR-053](../../adr/ADR-053-cloudpayments-webhook-user-resolution-via-auth-devices.md)) `user_not_found` логировал распарсенный `X` как `userId`; теперь `X`=кандидат, а не подтверждённый наш id — поэтому на `user_not_found` `userId` опускается.

## Инвариант «ровно один лог на вызов»
Каждая return-ветка проходит через `_log_outcome` один раз; `handle` НЕ логирует на пути `_apply`.

## Allowlist / запрет (PII, секреты)
**Логируется только:** `result`, `reason`, `transactionId`, `productId`, `userId` (**резолвнутый** наш UUID, `str`), `kind`, `resolvedVia` (`"user_id"`\|`"device_id"`, [ADR-053](../../adr/ADR-053-cloudpayments-webhook-user-resolution-via-auth-devices.md)). Опционально — `accountId` (= исходный `X`/deviceId, наш внутренний id, безопасно).
**ЗАПРЕЩЕНО:** карт-данные (`CardFirstSix`/`CardLastFour`/`Issuer`/`CardType`), `Authorization`/bearer/`CLOUDPAYMENTS_WEBHOOK_TOKEN`, сырой `raw`/`Data`-строка, `amount`/`currency` в логе (они только в санитизированном `payload`/audit, не в outcome-логе). Канон — [ADR-050 §7](../../adr/ADR-050-cloudpayments-webhook.md), [05-security.md](../../05-security.md#логирование-безопасное).

## Дополнение [ADR-054](../../adr/ADR-054-cloudpayments-webhook-payment-verification.md) — поля верификации/реконсиляции

Модель начисления сменилась на **реконсиляцию платежей** (`GET /users/{deviceId}/payments`). `outcome`-лог остаётся **один агрегатный на колбэк**, но:

**Новые поля `cloudpayments_webhook_outcome`:**
| Поле | Значение |
|---|---|
| `verify` | `"ok"` (2xx от broadapps) \| `"api_error"` (таймаут/не-2xx/malformed). Опущено на до-verify исходах (`user_not_found`/ранние `ignored`). |
| `creditedCount` | int — сколько `payment_id` начислено на этом колбэке (`applied`: ≥1; `duplicate`/`no_creditable_payment`: 0). |
| `paymentStatuses` | список фактических broadapps `status` из `data[]` (для калибровки [Q-054-1](../../99-open-questions.md); безопасно). Опущено, если verify не выполнялся. |
| `resolvedVia` | как в [ADR-053](../../adr/ADR-053-cloudpayments-webhook-user-resolution-via-auth-devices.md) (`user_id`/`device_id`). |

**Новые/изменённые `reason` и уровни:**
| `result` | `reason` | Level |
|---|---|---|
| `applied` | `None` (`creditedCount≥1`) | INFO |
| `duplicate` | все платежи уже начислены | INFO |
| `ignored` | `user_not_found` (без GET) | **WARNING** |
| `ignored` | `no_creditable_payment` (2xx, нет свежих `succeeded`) | **WARNING** |
| `ignored` | `unknown_product` / `unknown_payment_type` (платёж пропущен) | **WARNING** |
| `ignored` | `not_a_completed_payment` / `invalid_account_id` / `empty_body` / `invalid_json` / `not_an_object` | INFO (empty_body → DEBUG) |
| — (raise) | `api_error` → лог `verify=api_error` (WARNING) затем `500` retriable | **WARNING** |

**Отменено [ADR-054](../../adr/ADR-054-cloudpayments-webhook-payment-verification.md):** reason'ы `missing_transaction_id`/`missing_product_id`/`invalid_data`/`unknown_product`-по-паттерну из [ADR-050](../../adr/ADR-050-cloudpayments-webhook.md) для пути начисления не применяются (тело колбэка больше не источник продукта/суммы; классификация — по `product.payment_type` из verify). Идемпотентность/дедуп — по broadapps `payment_id` (не `TransactionId`). **ЗАПРЕЩЕНО** дополнительно: `CLOUDPAYMENTS_API_TOKEN`/Bearer, полное тело ответа verify, `paid_at`-значения с PII (только `status` безопасен).

## Тестовые ориентиры (для qa)
- На **каждый** исход — ровно одна запись `"cloudpayments_webhook_outcome"` с корректными `result`/`reason`/`level`.
- `user_not_found`, `unknown_product` → **WARNING**; `applied`/`duplicate`/технические `ignored` → INFO; `empty_body` → DEBUG.
- `transactionId`/`userId`/`productId` присутствуют там, где распарсены; отсутствуют (не `null`-ключ) на ранних reason'ах.
- `userId` — строка UUID; в записи **нет** карт-данных, bearer, сырого payload.
- **[ADR-053](../../adr/ADR-053-cloudpayments-webhook-user-resolution-via-auth-devices.md):** `X`(=`AccountId`) в `users` → `applied` с `resolvedVia="user_id"`, `userId=X`; `X` только в `auth_devices` (deviceId) → `applied` с `resolvedVia="device_id"`, `userId`=связанный `auth_devices[X].user_id` (**не** `X`); `X` ни там ни там → `user_not_found` (WARNING), без `resolvedVia`/`userId`.
