# Token Purchase — Testing

## Unit
- Маппинг `productId → credits`; неизвестный → `422`.
- Число кредитов берётся из `TOKEN_PRODUCTS`, не из тела (подмена в теле игнорируется).

## Integration
- `POST /v1/tokens/purchase` (STOREKIT_TEST_MODE): валидная транзакция → grant, `creditsAdded`/`newBalance` корректны.
- Идемпотентность: повторная отправка той же транзакции → `creditsAdded=0`, баланс не растёт.
- Разграничение: token-purchase grant и subscription grant не конфликтуют (разные `meta.source`); subscription-grant поведение не меняется.
- Невалидная транзакция → `422`/`400`; `userId` ≠ `sub` → `403` (`code=forbidden`).
- **Policy-guard ([Q-015-1](../../99-open-questions.md) = вариант B):** нет активной подписки → `403 {code: "subscription_required"}`, **ledger не записан** (grant не вызван); активная подписка → grant проходит. Проверка подписки выполняется **до** verify/grant и не нарушает идемпотентность (повтор подписчика → `creditsAdded=0`).

## E2E
- Включить в [09-e2e-testing.md](../../09-e2e-testing.md): подписчик покупает пакет → рост баланса → списание в credits-mode. Отдельный кейс: без активной подписки покупка → `403 subscription_required`, баланс не меняется ([Q-015-1](../../99-open-questions.md) = вариант B).
