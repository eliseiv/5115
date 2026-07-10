# Policy Engine — Architecture

## Состав
- `evaluate(state, mode)` — чистая функция, без I/O, детерминированная.
- `PolicyStateLoader` — собирает `PolicyState` из репозиториев (Subscription/Wallet/BYOK/users) одним батчем чтения.
- `effective(userId)` — вызывает loader + `evaluate` для обоих режимов → ответ `/policy/effective`.

## Алгоритм (из ADR-002)
```
if mode == byok:
    if subscription != active: -> subscription_required|subscription_expired
    if not byok_enabled: -> byok_disabled
    if byok_status != valid: -> byok_invalid
    -> allow
else: # credits
    if subscription == active:
        if credits == 0: -> credits_empty
        -> allow
    elif subscription == expired: -> subscription_expired
    else: # none
        if trial_used: -> trial_used
        -> allow (trial)
```

## Кэширование
- `effective` может кэшироваться в Redis на короткий TTL (напр. 5s) по userId для снижения нагрузки UI-поллинга; инвалидация при subscription-sync / wallet consume / byok change.
- `evaluate` в `/chat/run` всегда на свежем state (без кэша) — критичность корректности.

## Чистота
- `evaluate` не пишет в БД. Переключение `trial_used` выполняет use-case `/chat/run` атомарно при фактической выдаче trial (ADR-005), не Policy Engine.
