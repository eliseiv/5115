# Website Builder — Overview

## Назначение
Claude в ходе обычного chat-диалога генерирует статический сайт (HTML/CSS/JS/ассеты). Backend **хранит** файлы
сайта и отдаёт **превью** по временному signed URL, работающему в любом браузере.

> **Опциональная фича ([ADR-022](../../adr/ADR-022-optional-project-and-tool-gating.md)).** Основной поток сервиса — чат-агрегатор без проекта. Website-builder (server-side `site.*`) активируется **только** когда сессия создана с `projectId`; без проекта `site.*` Claude не предлагаются.

## Scope (этот проход)
- Хранение: таблицы `projects` + `site_files` (контент в БД на старте, лимиты размера/числа файлов).
- Server-side tools (`site.*`), исполняемые **backend'ом** в tool-loop ([ADR-011](../../adr/ADR-011-server-side-tools.md)):
  - `site.write_file` (MUTATING) — записать/перезаписать файл сайта в хранилище.
  - `site.preview` — получить временный signed URL превью проекта.
  - `site.list` (read) — перечислить файлы проекта.
  - `site.read` (read) — прочитать файл проекта.
  - `site.delete` (MUTATING) — удалить файл проекта.
- Превью-эндпоинт `GET /v1/preview/{projectId}/{token}/{path:path}` — отдача статики с threat-model защитой
  ([ADR-010](../../adr/ADR-010-backend-hosted-preview.md)).

## Out of scope
- **Деплой / публикация в публичный интернет** — отложен (решение пользователя). Только генерация + хранение + превью.
- Object-storage для контента (на старте — БД; миграция — [TD-009](../../100-known-tech-debt.md)).
- Версионирование/история файлов сайта (последняя версия по `(project_id, path)`).
- Server-side рендеринг/сборка (генерируется статика как есть; нет npm-build на backend).
- Кастомные домены, TLS-сертификаты под превью.

## Бизнес-правила
- BR-WB-1: `site_files` принадлежат `project`, `project` принадлежит `user` (FK-цепочка). Доступ только владельца проекта
  (через сессию для tools; через подпись для превью).
- BR-WB-2: `site.*` исполняет **backend** немедленно в tool-loop, **не** отдавая клиенту как `tool_call`
  ([ADR-011](../../adr/ADR-011-server-side-tools.md)). `files.*`/`calendar.*`/`reminders.*` — по-прежнему iOS-client-side.
- BR-WB-3: лимиты — файл ≤ 1 MB, проект ≤ 10 MB, ≤ 200 файлов (конфигурируемо). Превышение → tool возвращает `is_error`.
- BR-WB-4: превью авторизуется signed URL (HMAC + TTL), а не пользовательским JWT; изоляция владельца «запечена» в подпись.
- BR-WB-5: генерация — обычный chat-шаг, биллинг по [ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md)
  (1 кредит = 1 сообщение) без изменений. Хранение/превью **не** тарифицируются отдельно (см. [Q-010-4](../../99-open-questions.md)).
