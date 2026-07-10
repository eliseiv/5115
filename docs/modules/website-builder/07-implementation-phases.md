# Website Builder — Implementation Phases

| Phase | Задача | Зависит от |
|---|---|---|
| WB-1 | Миграция Alembic (expand): таблицы `projects` + `site_files` + индексы ([04-data-model.md](04-data-model.md)). | DB |
| WB-2 | Config: `PREVIEW_URL_SECRET`, `PREVIEW_URL_TTL_SECONDS` (900), `PREVIEW_MAX_FILE_BYTES` (1MB), `PREVIEW_MAX_PROJECT_BYTES` (10MB), `PREVIEW_MAX_FILES` (200), `MAX_SERVER_TOOL_ROUNDS` (16), content-type allowlist. | — |
| WB-3 | Website Service: разрешение/создание проекта (`ux_projects_user_external` upsert), CRUD `site_files` с path-guard + лимитами; size консистентен с content. | WB-1, WB-2 |
| WB-4 | Signed URL: build (`exp`+HMAC под `PREVIEW_URL_SECRET`) + verify (constant-time, TTL). | WB-2 |
| WB-5 | Server-side tool-хэндлеры `site.write_file`/`site.preview`/`site.list`/`site.read`/`site.delete`; строгие Pydantic-схемы; `site.*` ∈ SERVER_SIDE_TOOLS; domain↔anthropic mapping; MUTATING → audit `tool_mutation`. | WB-3, WB-4 |
| WB-6 | Orchestrator tool-loop: ветвление client-side/server-side; server-side исполняется синхронно без round-trip к iOS; guard `MAX_SERVER_TOOL_ROUNDS`; `provider_tool_use_id` для server-side (ADR-008). | WB-5, Chat Orchestrator |
| WB-7 | Preview-роутер `GET /v1/preview/{projectId}/{token}/{path:path}`: verify подписи, изоляция владельца, path-guard, content-type из БД, security-заголовки (sandbox CSP, nosniff, X-Frame-Options, no-store), без JWT/cookies. | WB-3, WB-4 |
| WB-8 | Метрики: `site_file_write_total{result}`, `preview_request_total{result=ok|forbidden|not_found}`. | WB-5, WB-7 |

> Биллинг генерации — без изменений (обычный chat-шаг, [ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md)).
> Хранение/превью не тарифицируются ([Q-010-4](../../99-open-questions.md)). Object-storage — [TD-009](../../100-known-tech-debt.md).
