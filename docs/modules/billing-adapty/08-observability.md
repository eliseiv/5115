# billing-adapty / 08 — Observability (логирование исхода вебхука)

Реализует [ADR-046](../../adr/ADR-046-adapty-webhook-outcome-logging.md). Точное ТЗ для backend — без додумывания. Расширяет [03-architecture.md](03-architecture.md); HTTP-семантику, начисление, `KNOWN_EVENTS`, контракт `AdaptyWebhookResponse` и схему данных **не меняет**.

## Цель

Каждый вызов `AdaptyWebhookService.handle()` пишет **ровно одну** структурную лог-запись с исходом, чтобы причина `ignored` (сегодня видимая только в теле HTTP-ответа Adapty) была доступна оператору. Закрывает слепое пятно из инцидента (промо-подписка без начисления, исход `ignored`, причина нигде не залогирована).

## Файл для правки

Только `src/app/billing_adapty/service.py`. **Роутер `src/app/api_gateway/routers/billing_adapty.py` НЕ трогать** (иначе двойная запись; роутер не видит `event_id`/`customer_user_id`).

## Импорты и логгер (верх модуля сервиса)

```python
import logging
from app.observability.logging import log_event

logger = logging.getLogger(__name__)   # == "app.billing_adapty.service"
```

Паттерн идентичен `app.chat.orchestrator` (`logger` модульного уровня + `log_event(logger, level, message, **fields)`; образец — вызов `"policy_decision"`).

## Helper эмиссии

Добавить приватный метод (логирует и возвращает тот же `outcome` — чтобы вставлять в существующие `return`-точки без изменения control-flow):

```python
def _log_outcome(
    self,
    outcome: WebhookOutcome,
    *,
    event_type: str | None = None,
    event_id: str | None = None,
    customer_user_id: uuid.UUID | None = None,
) -> WebhookOutcome:
    level = _level_for(outcome.result, outcome.reason)
    log_event(
        logger,
        level,
        "adapty_webhook_outcome",
        result=outcome.result,
        reason=outcome.reason,
        eventType=event_type,
        eventId=event_id,
        customerUserId=str(customer_user_id) if customer_user_id is not None else None,
    )
    return outcome
```

Замечания:
- `customer_user_id` — `uuid.UUID` → **обязательно** `str(...)` (иначе `json.dumps` в `JsonFormatter` упадёт). `event_id` уже `str`.
- `None`-поля безопасны: `JsonFormatter` выкидывает ключи со значением `None` из итогового JSON (отсутствие = «не распарсено»). Ветвлений по `None` не требуется.
- `event_type` берётся из **параметра**, а не из `outcome.event_type`: на `duplicate`/`applied` поле `WebhookOutcome.event_type` равно `None`, но фактический тип события известен из `ParsedEvent`.

## Функция уровня

Модульная функция (или статометод). Уровень зависит только от `result` и `reason`:

```python
def _level_for(result: str, reason: str | None) -> int:
    if result in ("applied", "duplicate"):
        return logging.INFO
    # result == "ignored"
    if reason in ("user_not_found", "missing_customer_user_id"):
        return logging.WARNING
    if reason is None:
        # единственный ignored с reason=None — эхо неизвестного event_type
        return logging.WARNING
    if reason == "empty_body":
        return logging.DEBUG
    # invalid_json | not_an_object | missing_event_id
    return logging.INFO
```

Полная decision-таблица и её обоснование — [ADR-046 §Таблица уровней](../../adr/ADR-046-adapty-webhook-outcome-logging.md#таблица-уровней-resultreason--level). Кратко:

| `result` | `reason` | Level |
|---|---|---|
| `applied` | `None` | INFO |
| `duplicate` | `None` | INFO |
| `ignored` | `user_not_found` | WARNING |
| `ignored` | `missing_customer_user_id` | WARNING |
| `ignored` | `None` (неизвестный `event_type`) | WARNING |
| `ignored` | `empty_body` | DEBUG |
| `ignored` | `invalid_json` | INFO |
| `ignored` | `not_an_object` | INFO |
| `ignored` | `missing_event_id` | INFO |

## Точки вызова (каждый `return` → ровно один лог)

В `handle()` каждый ранний `return _ignored(...)` оборачивается в `_log_outcome(...)` с тем контекстом, который **уже распарсен** на этой точке. Финальная строка `return await self._apply(parsed, body)` **остаётся как есть** — `_apply` логирует свои исходы сам (иначе двойная запись).

| Точка в `handle()` | Исход | `event_id` | `customer_user_id` | `event_type` |
|---|---|---|---|---|
| `not raw` | `ignored/empty_body` | `None` | `None` | `None` |
| `json.loads` упал | `ignored/invalid_json` | `None` | `None` | `None` |
| `not isinstance(body, dict)` | `ignored/not_an_object` | `None` | `None` | `None` |
| `event_id is None` | `ignored/missing_event_id` | `None` | `None` | `None` |
| `customer_user_id is None` | `ignored/missing_customer_user_id` | `event_id` | `None` | `None` (или `event_type`, если распарсен раньше — рекоменд. [ADR-047](../../adr/ADR-047-adapty-real-payload-format-and-grant-idempotency.md)) |
| `not _user_exists(...)` | `ignored/user_not_found` | `event_id` | `customer_user_id` | `event_type` |
| `event_type not in KNOWN_EVENTS` | `ignored` (эхо `event_type`) | `event_id` | `customer_user_id` | `event_type` |
| `return await self._apply(...)` | — | — | — | (логирует `_apply`) |

Примечание к точке `missing_customer_user_id`: исходно `event_type` на ней ещё не распарсен — передаётся `event_type=None`. **[ADR-047](../../adr/ADR-047-adapty-real-payload-format-and-grant-idempotency.md) РЕКОМЕНДУЕТ** перенести `parse_event_type(body)` (чистая операция, без БД) **выше** проверки `customer_user_id` и передавать `event_type or None` в этот лог — тогда WARNING несёт реальный тип события («`trial_started` пришёл, но нет `customer_user_id`» вместо безликого `missing_customer_user_id`); это типичный текущий прод-исход, пока iOS не вызвал `Adapty.identify`. `event_id` (=`profile_event_id`, [ADR-047](../../adr/ADR-047-adapty-real-payload-format-and-grant-idempotency.md)) уже известен — передать его. Не меняет HTTP-семантику/дедуп/уровни.

Пример обёртки (точка `user_not_found`):
```python
if not await self._user_exists(customer_user_id):
    return self._log_outcome(
        _ignored("user_not_found"),
        event_type=event_type, event_id=event_id, customer_user_id=customer_user_id,
    )
```

В `_apply()` обе returning-точки маршрутизируются через `_log_outcome` с данными `event` (`ParsedEvent` несёт `event_id`/`event_type`/`customer_user_id`):

| Точка в `_apply()` | Исход | поля |
|---|---|---|
| `inserted is None` | `duplicate` | `event_type=event.event_type, event_id=event.event_id, customer_user_id=event.customer_user_id` |
| хвост метода | `applied` | то же |

```python
if inserted is None:
    return self._log_outcome(
        WebhookOutcome(result="duplicate"),
        event_type=event.event_type, event_id=event.event_id,
        customer_user_id=event.customer_user_id,
    )
...
return self._log_outcome(
    WebhookOutcome(result="applied"),
    event_type=event.event_type, event_id=event.event_id,
    customer_user_id=event.customer_user_id,
)
```

## Инвариант «ровно один лог на вызов»

Каждая return-ветка проходит через `_log_outcome` **один раз**; `handle` НЕ логирует на пути `_apply` (логирует сам `_apply`). Двойной записи нет, пропусков нет.

## Allowlist / запрет (PII, секреты)

**Логируется только:** `result`, `reason`, `eventType`, `eventId`, `customerUserId` (наш UUID, `str`). **Запрещено:** сырой `raw`/`body`, `Authorization`/bearer/`ADAPTY_WEBHOOK_SECRET`, любые поля payload вне allowlist (`vendor_product_id`, `expires_at`, `profile.*` и пр.). Канон — [ADR-046 §Allowlist](../../adr/ADR-046-adapty-webhook-outcome-logging.md#allowlist-полей-лога-pii--секреты), [05-security.md](../../05-security.md#логирование-безопасное). Имена полей не пересекаются с redaction-denylist → значения не маскируются.

## Тестовые ориентиры (для qa)

- На **каждый** исход (`empty_body`/`invalid_json`/`not_an_object`/`missing_event_id`/`missing_customer_user_id`/`user_not_found`/unknown-type/`duplicate`/`applied`) — ровно одна запись `"adapty_webhook_outcome"` с корректными `result`/`reason`/`level`.
- `user_not_found`, `missing_customer_user_id`, unknown `event_type` → **WARNING**; `applied`/`duplicate`/`invalid_json`/`not_an_object`/`missing_event_id` → **INFO**; `empty_body` → **DEBUG** (через `caplog.set_level(logging.DEBUG)`).
- `eventId`/`customerUserId` присутствуют там, где распарсены; **отсутствуют** (не `null`-ключ) на ранних reason'ах.
- `customerUserId` — строка UUID, не объект.
- В записи **нет** сырого payload и bearer-секрета (проверить отсутствие `Authorization`/тела).
- HTTP-ответ и код не изменились относительно [02-api-contracts.md](02-api-contracts.md) (логирование — побочный эффект).
