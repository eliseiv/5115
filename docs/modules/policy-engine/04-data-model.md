# Policy Engine — Data Model

Не владеет таблицами. Читает (read-only):
- `subscriptions(status, expires_at)`
- `wallets(balance)`
- `byok_keys(enabled, key_status)`
- `users(trial_used)`

## Производные значения
- `isSubscribed` = `status==active AND (expires_at IS NULL OR expires_at > now())`.
- При `status==active`, но `expires_at <= now()` — состояние трактуется как `expired` (ленивое истечение; Subscription Service нормализует статус при sync). См. [Q-007-1](../../99-open-questions.md).

## Redis (опционально)
- `policy:eff:<userId>` — кэш ответа `/policy/effective`, короткий TTL.
