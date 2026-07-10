# Website Builder — Security / Threat Model

Дополняет корневой [05-security.md](../../05-security.md). Критично: превью отдаёт **пользовательский (Claude-сгенерированный)
HTML/JS**, исполняемый в браузере. Базовое решение и модель — [ADR-010](../../adr/ADR-010-backend-hosted-preview.md).

## Секрет
- `PREVIEW_URL_SECRET` — отдельный высокоэнтропийный секрет (≥ 32 байта), **не пересекается** с `JWT`-ключами, `KMS`,
  `ANTHROPIC_API_KEY`, `ADMIN_API_SECRET`. Только env / secret manager, никогда в коде/образе. Ротация: при смене секрета
  ранее выданные URL инвалидируются (приемлемо — TTL короткий).

## Абсолютный preview-URL (ADR-031) — модель угроз НЕ меняется
- С [ADR-031](../../adr/ADR-031-absolute-preview-url.md) `site.preview` отдаёт **абсолютный** URL `https://<SERVICE_DOMAIN>/v1/preview/...` вместо относительного пути (фикс галлюцинации хоста моделью).
- Это **не ослабляет** контур: авторизация целиком в **подписи** (HMAC+TTL, owner-isolation ниже), **не в хосте**. Тот же signed token, тот же TTL, тот же path-traversal guard и content-type allowlist. preview-роутер `GET /v1/preview/...` и верификация **не изменены**.
- `SERVICE_DOMAIN` — PUBLIC (доменное имя, уже в Traefik-лейблах), не секрет; добавление его в чтение app-config новых секретов не вводит.

## Signed URL (HMAC + TTL)
- `token = base64url(exp) . base64url(HMAC_SHA256(PREVIEW_URL_SECRET, "projectId|ownerUserId|exp"))`.
- Проверка: HMAC constant-time (`hmac.compare_digest`) + `exp` не истёк. Любое несовпадение/истечение → `403`.
- TTL дефолт `PREVIEW_URL_TTL_SECONDS=900` (15 мин), конфигурируемо ([Q-010-1](../../99-open-questions.md)).
- Подпись связывает токен с конкретными `projectId` **и** `ownerUserId` — подделать доступ к чужому проекту нельзя
  (изменение любого поля ломает HMAC).

## Изоляция по владельцу/проекту
- `site_files` → `projects` → `users` (FK). Превью читает только `projectId` из подписи; сверяет `projects.user_id` с
  `ownerUserId` подписи. Несовпадение → `403`; несуществующий проект/файл → `404` (не раскрывать существование чужого).

## Защита отдаваемого контента
| Угроза | Митигирование |
|---|---|
| Подделка/истечение URL | HMAC под отдельным `PREVIEW_URL_SECRET` + TTL; constant-time проверка; `403`. |
| Чтение чужого проекта | `ownerUserId` запечён в подпись; сверка с `projects.user_id`; `404` для чужого/несуществующего. |
| Path-traversal | Нормализация `path`; запрет `..`/абсолютных/`\`/NUL; lookup по `(project_id, path)` в БД, не по ФС; `404` при несовпадении. |
| MIME-sniffing / подмена типа | `X-Content-Type-Options: nosniff`; content-type строго из `site_files.content_type` (allowlist), не из расширения/заголовков запроса. |
| XSS/доступ к API-origin из пользовательского JS | `Content-Security-Policy: sandbox allow-scripts allow-forms; default-src 'self'`; никаких cookie/credentials на превью; рекомендация отдельного поддомена ([Q-010-3](../../99-open-questions.md)). |
| Clickjacking/встраивание | `X-Frame-Options: SAMEORIGIN` + `frame-ancestors 'self'`. |
| Кража сессии через превью | Эндпоинт не выставляет/не читает cookies, не участвует в auth-сессии; `Cache-Control: private, no-store`. |
| Раздувание БД / DoS хранилищем | Лимиты: файл ≤ `PREVIEW_MAX_FILE_BYTES` (1 MB), проект ≤ `PREVIEW_MAX_PROJECT_BYTES` (10 MB), файлов ≤ `PREVIEW_MAX_FILES` (200); проверка до вставки в `site.write_file`; превышение → tool `is_error`. |
| Запись в чужой проект моделью | `userId`/`external_project_id` берутся из серверного контекста шага, **не** из tool-args. |

## Content-type allowlist
Разрешено к хранению/отдаче: `text/html`, `text/css`, `text/javascript`, `application/json`, `image/png`, `image/jpeg`,
`image/svg+xml`, `image/gif`, `image/webp`, `font/woff2`, `text/plain`. Тип вне allowlist → запись отклоняется
(`site.write_file` → `is_error` / `422`). Точный список конфигурируем ([Q-010-2](../../99-open-questions.md)).

## Изоляция origin (решение)
- Старт: выделенный путь `/v1/preview/*` + sandbox-заголовки (самодостаточно для single-origin).
- Prod-рекомендация (операционная): отдельный поддомен `preview.<domain>` → даже при обходе CSP пользовательский JS
  не имеет same-origin доступа к API. Зафиксировано как [Q-010-3](../../99-open-questions.md) (не блокер).

## Что НЕ применяется (scope)
- Деплой/публичный хостинг отложен — превью не публичный CDN.
- Нет исполнения пользовательского кода на backend (только хранение/отдача статики; нет SSR/build).
