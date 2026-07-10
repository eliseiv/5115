# Module: Website Builder

- Статус: Реализован (**опциональная фича**, [ADR-022](../../adr/ADR-022-optional-project-and-tool-gating.md))
- Ответственность: хранение сгенерированных Claude статических сайтов (`projects`/`site_files`) и backend-hosted превью по временному signed URL. Server-side tools `site.*`, исполняемые backend'ом в tool-loop.
- **Позиционирование:** основной поток сервиса — чат-агрегатор без проекта; website-builder — необязательная фича, активируется **только** когда сессия создана с `projectId` (иначе `site.*` Claude не предлагаются, [ADR-022](../../adr/ADR-022-optional-project-and-tool-gating.md)).

## Документы
- [00-overview.md](00-overview.md)
- [01-context.md](01-context.md)
- [02-api-contracts.md](02-api-contracts.md) — server-side tools `site.*` + preview-эндпоинт
- [03-architecture.md](03-architecture.md)
- [04-data-model.md](04-data-model.md)
- [05-security.md](05-security.md) — threat model превью (отдаём пользовательский HTML/JS)
- [06-rbac.md](06-rbac.md)
- [07-implementation-phases.md](07-implementation-phases.md)
- [09-testing.md](09-testing.md)

## DoD
- Таблицы `projects` + `site_files` (изоляция: file→project→user), лимиты размера/числа файлов.
- Server-side tools `site.write_file`/`site.preview` (+ опц. `site.list`/`site.read`/`site.delete`): исполняет **backend**,
  продолжает tool-loop без round-trip к iOS ([ADR-011](../../adr/ADR-011-server-side-tools.md)). Строгие Pydantic-схемы, domain↔anthropic mapping, `site.write_file`/`site.delete` ∈ MUTATING (audit).
- `GET /v1/preview/{projectId}/{token}/{path:path}`: HMAC signed URL + TTL ([ADR-010](../../adr/ADR-010-backend-hosted-preview.md)),
  изоляция по владельцу, path-traversal guard, content-type allowlist, sandbox security-заголовки, no cookies/credentials.
- Генерация — обычный chat-шаг (1 кредит = 1 сообщение, [ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md) без изменений); хранение/превью не тарифицируются.

## Changelog
- 2026-06-01: bootstrap модуля (architect). [ADR-010](../../adr/ADR-010-backend-hosted-preview.md) (backend-hosted preview + threat model), [ADR-011](../../adr/ADR-011-server-side-tools.md) (server-side tools). Новые таблицы `projects`/`site_files`. Scope backend.
- 2026-06-01: реализован backend (`src/app/website/*`, `src/app/api_gateway/routers/preview.py`, миграция `0003`): server-side tools `site.*` в tool-loop, `GET /v1/preview/{projectId}/{token}/{path}` с HMAC signed URL/TTL/sandbox-заголовками, таблицы `projects`/`site_files`, лимиты, content-type allowlist. Отревьюен и протестирован — offline-сьют зелёный (455/455, вкл. e2e preview write→signed-URL→serve). Live-прогон с реальным Claude ожидает пополнения баланса Anthropic (внешнее ограничение, не дефект кода). Хранение контента в БД → [TD-009](../../100-known-tech-debt.md). Статус → «Реализован».
- 2026-06-16: [ADR-031](../../adr/ADR-031-absolute-preview-url.md) — `site.preview` возвращает **абсолютный** URL `https://<SERVICE_DOMAIN>/v1/preview/...` вместо относительного пути (фикс прод-бага: модель галлюцинировала чужой хост `claude.site`/`val.town`). `SERVICE_DOMAIN` читается app-config (`config.py`, с нормализацией), пустой → fallback на относительный путь ([TD-022](../../100-known-tech-debt.md)). Описание `site.preview` дополнено «использовать URL как есть». Контракт — [02-api-contracts.md](02-api-contracts.md#sitepreview-utility); модель угроз неизменна ([05-security.md](05-security.md)). Scope backend.
