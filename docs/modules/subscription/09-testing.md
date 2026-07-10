# Subscription — Testing

## Unit
- Нормализация статуса: expiresDate в прошлом/будущем, revoked → active/expired.

## Integration (respx для App Store Server API)
- Валидная транзакция → status=active, expiresAt, plan; upsert корректен.
- Поддельная/невалидная подпись → 422, subscription не изменена.
- Повторный sync той же транзакции → grant не дублируется (идемпотентность по transactionId).
- refund/revocation → status=expired, isSubscribed=false.
- Истёкшая подписка → Policy Engine отдаёт subscription_expired для chat (интеграция с AC-2).
- audit subscription_change создаётся.
