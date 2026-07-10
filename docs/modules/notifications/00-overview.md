# Notifications — Overview

## Назначение
Toggle уведомлений в настройках + регистрация устройства для push (APNs). На старте — только хранение настройки и device-токена; фактическая доставка push отложена ([TD-011](../../100-known-tech-debt.md)).

## Scope (этот проход)
- `POST /v1/notifications/device-token` — зарегистрировать/обновить APNs device-токен для `(user, device)`.
- `DELETE /v1/notifications/device-token` — удалить токен устройства (отписка / logout).
- Настройка `notificationsEnabled` — читается/пишется через preferences (`PATCH /v1/preferences`), отдельного toggle-эндпоинта здесь нет (единый источник — `user_preferences`).

## Out of scope ([TD-011](../../100-known-tech-debt.md))
- APNs-клиент (token-based JWT auth к Apple Push), отправка push.
- Триггеры доставки (например push по завершении длинной генерации).
- In-app notification center / история уведомлений.

## Бизнес-правила
- BR-NT-1: один токен на `(user_id, device_id)` — upsert при перерегистрации (`ux_push_tokens_user_device`).
- BR-NT-2: `device_id` берётся из JWT-claim / `X-Device-Id` (как в gateway). Токен принадлежит `sub`.
- BR-NT-3: при будущей отправке push (TD-011) — уважать `user_preferences.notifications_enabled` (выключено → не слать).
- BR-NT-4: `push_token` — не секрет уровня ключа, но обрабатывается как чувствительный идентификатор устройства; не светится в общих логах сверх необходимости.
