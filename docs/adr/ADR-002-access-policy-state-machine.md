# ADR-002 — Политика доступа как state machine (trial → subscription → credits/byok)

- Статус: Accepted
- Дата: 2026-05-21

## Context
Бизнес-правила доступа (BR-1..BR-5 в [00-vision.md](../00-vision.md)) нетривиальны и должны давать **один и тот же** результат в `/v1/chat/run` и `/v1/policy/effective` (AC-6). Нужна детерминированная, тестируемая, единая точка решения.

## Decision
Единый **Policy Engine** — чистая функция без побочных эффектов:

```
evaluate(state) -> Decision
```

Вход `state`:
- `subscription_status` ∈ {none, active, expired}
- `trial_used` ∈ {true, false}
- `credits_balance` ∈ {0, >0}
- `byok` ∈ {missing, disabled, invalid, valid}
- `mode` ∈ {credits, byok}

Выход `Decision`: `allow=true` **или** `allow=false` + `blockReason` (enum).

### Алгоритм (порядок проверок фиксирован)
```
if mode == byok:
    if subscription_status != active: -> subscription_required (если none) | subscription_expired (если expired)   # BR-5
    if byok == disabled:  -> byok_disabled
    if byok in {missing, invalid}: -> byok_invalid
    -> allow
else:  # mode == credits
    if subscription_status == active:
        if credits_balance == 0: -> credits_empty        # BR-3
        -> allow (списание после генерации)
    else:  # none или expired -> без активной подписки
        if subscription_status == expired: -> subscription_expired   # BR-5
        # subscription_status == none:
        if trial_used: -> trial_used                     # BR-1
        -> allow (это и есть единственный trial; trial_used станет TRUE)
```

> Примечание: `rate_limited` определяется на API Gateway до Policy Engine; `policy_denied` — общий fallback на непредвиденное состояние.

### Таблица переходов (ключевые комбинации)
| subscription | trial_used | credits | byok | mode | результат |
|---|---|---|---|---|---|
| none | false | — | — | credits | **allow (trial)** → set trial_used |
| none | true | — | — | credits | block `trial_used` |
| none | — | — | valid | byok | block `subscription_required` |
| active | — | 0 | — | credits | block `credits_empty` |
| active | — | >0 | — | credits | **allow** + debit |
| active | — | — | valid | byok | **allow** |
| active | — | — | disabled | byok | block `byok_disabled` |
| active | — | — | invalid/missing | byok | block `byok_invalid` |
| expired | — | >0 | valid | credits | block `subscription_expired` |
| expired | — | — | valid | byok | block `subscription_expired` |

## Consequences
- (+) Один источник истины → `/policy/effective` и `/chat/run` консистентны by construction (AC-6).
- (+) Чистая функция → исчерпывающее параметризованное тестирование (см. [06-testing-strategy.md](../06-testing-strategy.md)).
- (+) Машиночитаемый `blockReason` для UI.
- (−) Trial — состояние пользователя (`users.trial_used`), а не подписки; требует атомарного перехода (см. ADR-005).

## Trial concurrency (осознанно принятый риск)
Policy Engine — чистая функция, вычисляющая решение **до** генерации и **до** flip `users.trial_used`. Поэтому два параллельных первых `/v1/chat/run` одного пользователя (subscription=none, trial_used=false, mode=credits) могут оба получить policy-allow и оба сгенерировать бесплатный ответ — окно двойной бесплатной генерации.

Это **не** биллинговая утечка:
- Flip trial атомарен и идемпотентен: `UPDATE users SET trial_used=TRUE WHERE trial_used=FALSE` (ADR-005). Двойного flip нет.
- Двойного списания нет: trial-allow не списывает кредиты вовсе (debit отсутствует на этой ветке), а кредитные списания защищены idempotency-ledger (ADR-005).

Худший исход — **1 лишняя бесплатная генерация** на пользователя при точной гонке двух его первых запросов. Стоимость пренебрежима, поэтому риск принят осознанно и зарегистрирован как [TD-006](../100-known-tech-debt.md). Митигирование (advisory lock / `SELECT ... FOR UPDATE` по `users` на ветке trial-allow) описано в TD-006 и активируется при появлении заметного злоупотребления по метрикам — это не требование на текущем этапе.

## Alternatives
- Ad-hoc проверки в каждом endpoint — отвергнуто: дублирование, риск рассинхрона `/policy/effective` ↔ `/chat/run`.
- Хранить состояние FSM явно в БД — отвергнуто: состояние выводится из текущих subscription/wallet/byok/trial, отдельная таблица избыточна.
