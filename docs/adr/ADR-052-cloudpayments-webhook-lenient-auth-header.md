# ADR-052 — Терпимый разбор заголовка авторизации вебхука CloudPayments + диагностический лог на 401

- Статус: Accepted
- Дата: 2026-07-03
- Тип: bugfix / implementation ADR. **Исправляет [ADR-050 §1](ADR-050-cloudpayments-webhook.md)** (авторизация входящего вебхука). Тело ADR-050 не переписывается (immutability); актуальное поведение приёма токена — здесь.
- Связано: [ADR-050](ADR-050-cloudpayments-webhook.md) (RU-вебхук broadapps), [ADR-029](ADR-029-adapty-subscription-webhook.md) (образец bearer-вебхука — **НЕ трогается**), [ADR-046](ADR-046-adapty-webhook-outcome-logging.md) (образец структурного лога исхода), [ADR-009](ADR-009-admin-token-auth.md) (constant-time секрет). Модуль [billing-cloudpayments](../modules/billing-cloudpayments/README.md).

## Context

**Инцидент (прод avelyra).** Реальные колбэки broadapps на `POST /v1/billing/cloudpayments/webhook` отбиваются с `401`, начисления не происходит.

**Диагностика.** Значение секрета в `.env` (`CLOUDPAYMENTS_WEBHOOK_TOKEN`, 64 символа) сверено с тем, что должен слать broadapps (заказчик прислал копипастом) — **совпадает точь-в-точь**. Значит проблема **не в значении секрета, а в формате заголовка `Authorization`**.

**Корень.** Текущий верификатор [`src/app/billing_cloudpayments/auth.py::require_cloudpayments_webhook`](../../src/app/billing_cloudpayments/auth.py) извлекает токен через FastAPI `HTTPBearer` (`cloudpayments_webhook_scheme`, `auto_error=False`). `HTTPBearer` **требует** ровно схему `Authorization: Bearer <token>`: если слово-схема не `Bearer` или заголовок «сырой» (`Authorization: <token>` без префикса), `HTTPBearer` возвращает `None` → `credentials is None` → `401`. broadapps — партнёрский отправитель с **нефиксированным** форматом; наиболее вероятно он шлёт токен без префикса `Bearer` (или в ином виде), и мы отбиваем валидный секрет.

**Слепое пятно.** На `401` мы ничего не логируем о том, **как** пришёл заголовок, поэтому не можем подтвердить гипотезу и не увидим, если broadapps шлёт токен в **другом** заголовке (напр. `X-Api-Key`) или как HMAC-подпись.

## Decision

Сделать приём токена вебхука **терпимым к формату** (lenient-parse), **без снижения безопасности** (сравнение остаётся constant-time, поведение fail-closed), и добавить **безопасный диагностический лог на 401**. Изменение затрагивает **только** CloudPayments-вебхук. **Adapty-вебхук ([ADR-029](ADR-029-adapty-subscription-webhook.md)/[ADR-046](ADR-046-adapty-webhook-outcome-logging.md)/[ADR-047](ADR-047-adapty-real-payload-format-and-grant-idempotency.md)) и любая прочая auth (JWT / admin / preview) НЕ трогаются.**

### 1. Терпимый разбор `Authorization` (только CloudPayments-вебхук)

Верификатор читает **сырой** заголовок `request.headers.get("authorization")` (вместо доверия извлечению `HTTPBearer`) и извлекает credential по правилу:

1. Заголовок отсутствует или пуст (после `strip`) → credential = `None`.
2. Разбить по первой группе пробелов (`value.split(None, 1)`):
   - **Две части** и первое слово (case-insensitive) ∈ {`bearer`, `token`} → credential = вторая часть (после `strip`). Покрывает `Bearer <token>` (регистронезависимо к слову Bearer) и `Token <token>`.
   - **Две части**, но первое слово — иная схема (`Basic`, `Signature`, …) → credential = **весь** trimmed-заголовок (не распознанная схема не отрезается) → не совпадёт с секретом → `401` (fail-closed).
   - **Одна часть** (нет пробелов) → «сырой» токен → credential = весь trimmed-заголовок. Покрывает `Authorization: <token>` без схемы.

Формально извлечение — чистая функция `_extract_webhook_credential(authorization: str | None) -> str | None`.

### 2. Сравнение и семантика ответов (сохраняются)

- Секрет `CLOUDPAYMENTS_WEBHOOK_TOKEN` **не задан** (`""`) → `500` (`CloudPaymentsWebhookMisconfiguredError`, «cloudpayments webhook token not configured»). Пустой секрет не матчит ни один presented-токен ⇒ эндпоинт активен только на avelyra. **Без изменений.**
- Сравнение **constant-time** — `hmac.compare_digest(candidate, secret)`, где `candidate = _extract_webhook_credential(header) or ""`. **Оба** пути — «нет заголовка» (`candidate == ""`) и «неверный токен» — **всегда** выполняют `compare_digest` (не короткозамыкать по `candidate is None`), чтобы не было ветвевого timing-отличия между «нет заголовка» и «неверный токен». Оба исхода → одинаковый `401` (`UnauthorizedError`, причина не раскрывается).
- Токен/секрет **никогда** не логируются и не попадают в ответ.

### 3. Наблюдаемость — безопасный диагностический лог на 401

Ровно **одна** структурная запись `"cloudpayments_webhook_auth_denied"` (уровень **WARNING**) эмитится в `require_cloudpayments_webhook` **только на `401`** (mismatch/нет заголовка). На `500` (misconfigured) и на успех — не эмитится. Логгер модуля `app.billing_cloudpayments.auth` через `log_event` (образец [ADR-046](ADR-046-adapty-webhook-outcome-logging.md)).

**Allowlist полей (что МОЖНО):**
- `matched: bool` — всегда `false` на этом пути (для явности/симметрии; ключевой сигнал добивания).
- `authScheme: str` — **только слово-схема**, не значение:
  - заголовка нет → `"none"`; пустой → `"empty"`;
  - две части (схема + значение) → первое слово в lower (`"bearer"`/`"token"`/`"basic"`/…) — безопасно, т.к. сам токен во второй части;
  - одна часть (нет пробелов, «сырой» токен) → `"raw"` — **значение НЕ логируется**.
- `presentAuthHeaders: list[str]` — **имена** (не значения) присутствующих заголовков из фиксированного allowlist: `("authorization","x-api-key","x-signature","x-sign","x-webhook-signature","x-content-hmac","content-hmac","signature")`. Цель — сразу увидеть, если broadapps шлёт секрет в **другом** заголовке / как подпись.

**ЗАПРЕЩЕНО логировать:** значение токена/секрета, полный заголовок `Authorization`, значения любых заголовков, сырое тело. Только имена + слово-схема + `matched`.

### 4. OpenAPI / Swagger «Authorize» (сохранение лока)

`cloudpayments_webhook_scheme` (`HTTPBearer`, `auto_error=False`, `scheme_name="cloudPaymentsWebhook"`) **остаётся** как **декоративная** `SecurityBase`-зависимость в сигнатуре `require_cloudpayments_webhook` (неиспользуемый параметр `_scheme`), чтобы операция сохраняла security-схему (иконку-замок и кнопку Authorize) в OpenAPI. **Реальная проверка больше не берёт credential из этого извлечения** — она читает сырой заголовок (§1). Так лок в Swagger не исчезает, а верификация становится формат-терпимой. `description` схемы обновляется: секрет можно ввести с префиксом `Bearer`/`Token` **или** без схемы (сырым). Дополнительного `authorization`-параметра в операции не появляется (`SecurityBase` не добавляет parameter).

### 5. Безопасность (инварианты)

- Constant-time сравнение сохранено; fail-closed (любая неоднозначность формата, не сводящаяся к точному совпадению секрета, → `401`).
- Нет timing-leak между «нет заголовка» и «неверный токен»: оба всегда проходят `compare_digest`, оба → `401`.
- Терпимость **только** к обёртке (`Bearer`/`Token`/сырой) — **значение** секрета сравнивается точно и целиком. Расширения аутентификации (иной заголовок, HMAC-подпись) **не** вводятся спекулятивно — сначала подтверждаем формат по диагностическому логу ([Q-052-1](../99-open-questions.md)).
- PII/секреты в диагностическом логе исключены by-design (§3 allowlist).

## Consequences

**Плюсы.**
- Валидный секрет broadapps принимается независимо от того, шлёт ли партнёр `Bearer <token>`, `Token <token>` или сырой `<token>` — устраняет прод-401 на корректном значении.
- Диагностический лог даёт немедленный сигнал для добивания: если формат окажется иным (другой заголовок / подпись), мы увидим это в `presentAuthHeaders`/`authScheme` и доработаем точечно, не гадая.
- Swagger-лок и изоляция контура сохранены; Adapty и прочая auth не затронуты.

**Минусы / риски.**
- Терпимость к обёртке немного расширяет surface приёма, но **значение** секрета по-прежнему сверяется constant-time целиком — энтропия секрета (64 симв.) не снижается.
- WARNING-лог на 401 может шуметь при интернет-сканерах, бьющих по эндпоинту. Путь не публичен/не очевиден, объём низкий; при потребности снизить уровень/добавить троттлинг — отдельной правкой (не блокер).

## Alternatives

- **A. Просить заказчика переключить broadapps на `Bearer <token>`.** Отклонено как единственное решение: формат партнёра не под нашим контролем и может дрейфовать; терпимый приём надёжнее и обратно совместим (`Bearer` продолжает работать).
- **B. Полностью убрать `HTTPBearer`-схему и читать только сырой заголовок.** Отклонено: пропадёт security-схема `cloudPaymentsWebhook` в OpenAPI (замок/Authorize). Оставляем схему декоративной (§4).
- **C. Принимать секрет из произвольного заголовка / поддержать HMAC-подпись сразу.** Отклонено (преждевременно): не подтверждено, что broadapps так шлёт. Сначала диагностический лог ([Q-052-1](../99-open-questions.md)); при подтверждении — отдельный ADR.
- **D. Логировать полный заголовок для диагностики.** Отклонено: утечка секрета в лог. Логируем только имена + слово-схему + `matched` (§3).
