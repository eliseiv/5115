# Website Builder — Context

## Зависимости
| Зависит от | Зачем |
|---|---|
| Chat Orchestrator | tool-loop: исполнение server-side `site.*` tools в рамках `/chat/run` / `/chat/tool-result` ([ADR-011](../../adr/ADR-011-server-side-tools.md)) |
| PostgreSQL | таблицы `projects`, `site_files` |
| Audit | `tool_mutation` для `site.write_file`/`site.delete` |
| API Gateway | размещение публичного `GET /v1/preview/*` (signed URL, без JWT), security-заголовки превью |
| Config | `PREVIEW_URL_SECRET`, `PREVIEW_URL_TTL_SECONDS`, лимиты размера/числа файлов |

## Кто зависит
- Chat Orchestrator вызывает хэндлеры `site.*` синхронно в tool-loop.
- Браузер пользователя открывает превью по signed URL.

## Связь project ↔ session
- `chat_sessions.project_id` (TEXT, **nullable** с миграции `0007`, [ADR-022](../../adr/ADR-022-optional-project-and-tool-gating.md)) — клиентский идентификатор проекта в рамках диалога. `NULL` → «чистый чат» без website-builder: `site.*` не предлагаются Claude, этот модуль не задействуется. Непустая строка → website-builder активен для сессии.
- Новая таблица `projects` (UUID PK, `user_id` FK, `external_project_id` = строковый клиентский id) — backend-сущность
  хранилища сайта. Связь: server-side tool при первой записи **разрешает/создаёт** `projects`-строку для
  `(user_id, external_project_id)` сессии (см. [03-architecture.md](03-architecture.md#разрешение-проекта)).
- `site_files.project_id` → `projects.id` (внутренний UUID), не путать с `chat_sessions.project_id` (внешняя строка).

## Tool-классы (повтор ADR-011)
- **client-side** (iOS исполняет): `files.*`, `calendar.*`, `reminders.*` — backend отдаёт `status=tool_call`, ждёт `tool_result`.
- **server-side** (backend исполняет): `site.*` — backend исполняет немедленно, продолжает tool-loop без round-trip к iOS.

## Связанные ADR / вопросы
- [ADR-010](../../adr/ADR-010-backend-hosted-preview.md) — backend-hosted preview, signed URL, threat model.
- [ADR-011](../../adr/ADR-011-server-side-tools.md) — server-side tools `site.*`.
- [ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md) — биллинг генерации (без изменений).
- [Q-010-1](../../99-open-questions.md) (TTL signed URL), [Q-010-2](../../99-open-questions.md) (лимиты),
  [Q-010-3](../../99-open-questions.md) (origin превью), [Q-010-4](../../99-open-questions.md) (тарификация хранения), [TD-009](../../100-known-tech-debt.md) (object-storage).
