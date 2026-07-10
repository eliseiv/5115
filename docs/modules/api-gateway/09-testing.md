# API Gateway — Testing

## Unit
- JWT verify: валидный/просроченный/неверная подпись/неверный aud/iss → 200/401.
- Size-валидаторы: на границе и выше лимита → 413/422.
- Error mapping: исключения → корректный код и формат ошибки.

## Integration
- Rate limit (реальный Redis): N+1 запрос → 429; раздельные счётчики user/device/IP.
- `userId != sub` → 403.
- Correlation id: `X-Request-Id` пробрасывается/генерируется и присутствует в логах.
- `/ready`: при недоступном Redis/PG → 503.
- Redaction: `Authorization` и поля с секретами отсутствуют в логах.

## Прочее
- Маршрутизация: каждый `/v1/*` доходит до нужного use-case (мок use-case).
