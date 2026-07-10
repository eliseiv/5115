# Profile — Context

## Зависимости
- **API Gateway** — auth, lazy provisioning (`users` строка гарантированно есть к моменту GET/PATCH), размещение роутов `/v1/profile`.
- **users** таблица — единственный источник `display_name`/`created_at`/`id`.

## Соседи
- **preferences** — отдельный модуль настроек (default_assistant_mode, notifications). Профиль (имя/accountId) и preferences (настройки) — разные эндпоинты, не смешиваются.

## Границы
- Profile не трогает billing/policy/chat. Только чтение/запись `users.display_name` и вычисление `accountId`.
