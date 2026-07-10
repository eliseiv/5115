# Notifications — Architecture

## Размещение
Пакет `src/app/notifications/`: репозиторий над `device_push_tokens` + use-cases (register/delete token) + роутер `/v1/notifications/*`. Настройка `notifications_enabled` — в preferences.

## Регистрация токена
- `deviceId` резолвится: тело → JWT-claim → `X-Device-Id`; отсутствие → `422`.
- Upsert: `INSERT ... ON CONFLICT (user_id, device_id) DO UPDATE SET push_token, updated_at`.

## Будущая отправка (TD-011, не в этом проходе)
- APNs-клиент (token-based JWT, `APNS_KEY_ID`/`APNS_TEAM_ID`/`APNS_AUTH_KEY`/`APNS_TOPIC`).
- Перед отправкой — проверка `user_preferences.notifications_enabled`; выключено → skip.
- Триггеры (например завершение длинной генерации) — определяются при реализации TD-011.

## Инварианты
- Токен принадлежит `sub`; один на `(user, device)`.
- На старте нет фоновых задач/исходящих push — только синхронные CRUD токена.
- `push_token` обрабатывается как чувствительный идентификатор (минимизация в логах).
