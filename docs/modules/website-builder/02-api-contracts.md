# Website Builder — API Contracts

Две части: **(A) server-side tools `site.*`** (исполняет backend в tool-loop, не публичный HTTP для iOS) и
**(B) публичный preview-эндпоинт** (HTTP GET, signed URL).

---

## A. Server-side tools `site.*` (ADR-011)

Исполняет **backend** немедленно в tool-loop ([ADR-011](../../adr/ADR-011-server-side-tools.md)); **НЕ** отдаются клиенту
как `status=tool_call`. **Предлагаются Claude только при наличии `chat_sessions.project_id`** (сессия создана с `projectId`, [ADR-022](../../adr/ADR-022-optional-project-and-tool-gating.md)); в «чистом чате» отсутствуют. Строгие Pydantic v2 схемы (`extra='forbid'`). domain↔anthropic mapping — точка → подчёркивание
(дополняет таблицу [chat-orchestrator/02-api-contracts.md](../chat-orchestrator/02-api-contracts.md#имена-tools-доменный-ios-vs-anthropic-формат)).

| Domain-name (внутренний контракт) | Anthropic-name | Класс | Тип |
|---|---|---|---|
| `site.write_file` | `site_write_file` | server-side | **mutate** (audit) |
| `site.preview` | `site_preview` | server-side | utility |
| `site.list` | `site_list` | server-side | read |
| `site.read` | `site_read` | server-side | read |
| `site.delete` | `site_delete` | server-side | **mutate** (audit) |

> Контекст исполнения: backend знает `userId` (из сессии шага) и `external_project_id` (из `chat_sessions.project_id`
> текущей сессии). Эти значения **не** приходят в args от модели — берутся из серверного контекста шага, чтобы модель
> не могла записать в чужой проект. Args содержат только данные файла/пути.

### `site.write_file` (mutate)
- **Args:** `{ "path": string, "content": string, "contentType": string, "encoding": "utf8|base64" }`
- **Result:** `{ "path": string, "bytesWritten": int, "fileCount": int, "projectBytes": int }`
- Записывает/перезаписывает `site_files` по `(project_id, path)` (upsert). Проект разрешается/создаётся из серверного
  контекста (`userId` + `external_project_id`), см. [03-architecture.md](03-architecture.md#разрешение-проекта).
- `contentType` ∈ allowlist ([05-security.md](05-security.md#content-type-allowlist)); иначе tool `is_error`.
- `path` нормализуется и проверяется (без `..`/абсолютных/`\`/NUL); нарушение → `is_error`.
- Лимиты (файл/проект/число файлов) проверяются до вставки; превышение → `is_error` с машиночитаемым кодом
  (`file_too_large`/`project_too_large`/`too_many_files`).
- MUTATING → audit `tool_mutation` (eventType, payload: `projectId`, `path`, `bytesWritten`; без content целиком).

### `site.preview` (utility)
- **Args:** `{ }` (пусто; проект — из серверного контекста) или `{ "entry": string? }` (стартовый путь, дефолт `index.html`).
- **Result:** `{ "url": string, "expiresAt": "ISO8601" }`
- `url` — **АБСОЛЮТНЫЙ** URL на наш домен: `https://<SERVICE_DOMAIN>/v1/preview/{projectId}/{token}/{entry}` ([ADR-031](../../adr/ADR-031-absolute-preview-url.md)).
  Абсолютная форма нужна, чтобы модель копировала ссылку **дословно** и не достраивала/галлюцинировала базовый хост (раньше `url` был относительным `/v1/preview/...`, и модель подставляла чужой хост вроде `claude.site`/`val.town`).
  - `<SERVICE_DOMAIN>` берётся из app-config (`service_domain`, PUBLIC, [config.py](../../../src/app/config.py)) и **нормализуется** (срез протокола `https://`/`http://` и хвостового `/`), без двойных слешей. Схема — всегда `https`.
  - **Fallback (только локальная разработка):** если `SERVICE_DOMAIN` пуст → `url` — относительный путь `/v1/preview/{projectId}/{token}/{entry}` (как прежде), без хардкода `localhost`. На проде `SERVICE_DOMAIN` уже задан в `.env` обоих инстансов ([07-deployment.md](../../07-deployment.md)).
- Подпись/TTL **не меняются**: signed URL `GET /v1/preview/{projectId}/{token}/{entry}` (HMAC под `PREVIEW_URL_SECRET`, TTL
  `PREVIEW_URL_TTL_SECONDS`, дефолт 15 мин). `projectId` — внутренний UUID проекта владельца сессии. Подпись покрывает
  `projectId`+`ownerUserId`+`exp` ([ADR-010](../../adr/ADR-010-backend-hosted-preview.md)).

### `site.list` (read)
- **Args:** `{ }`
- **Result:** `{ "files": [ { "path": string, "contentType": string, "size": int } ], "fileCount": int, "projectBytes": int }`

### `site.read` (read)
- **Args:** `{ "path": string }`
- **Result:** `{ "path": string, "content": string, "encoding": "utf8|base64", "contentType": string, "size": int }`
- Несуществующий путь → `is_error` (`file_not_found`).

### `site.delete` (mutate)
- **Args:** `{ "path": string }`
- **Result:** `{ "path": string, "deleted": bool, "fileCount": int, "projectBytes": int }`
- MUTATING → audit `tool_mutation`.

### Общие правила tools
- Все схемы — Pydantic v2, `extra='forbid'`.
- `encoding`: `utf8` для текста, `base64` для бинарных (изображения/шрифты). Backend декодирует в `content` (BYTEA).
- Ошибки исполнения возвращаются как tool `is_error=true` (Claude видит и может скорректироваться), **не** как HTTP `5xx`
  пользователю — server-side tool исполняется внутри tool-loop.
- Идемпотентность re-entry: server-side tool-call логируется в `tool_calls` со статусом `completed` сразу
  (нет ожидания client `tool_result`); повторный проход того же шага не дублирует мутацию ([ADR-011](../../adr/ADR-011-server-side-tools.md) §4).

---

## B. GET /v1/preview/{projectId}/{token}/{path:path}
Публичная отдача статики проекта по signed URL. **Без** пользовательского JWT (авторизация — в подписи).

### Path params
- `projectId` — внутренний UUID проекта.
- `token` — `<exp>.<hmac>` (url-safe base64, без паддинга): `exp` (unix ts) + HMAC-SHA256 под `PREVIEW_URL_SECRET`
  над канон. строкой `projectId|ownerUserId|exp`.
- `path` — относительный путь файла внутри проекта (`{path:path}` — может содержать `/`).

### Response
- **200** — тело файла, `Content-Type` из `site_files.content_type`. Заголовки безопасности (см. ниже).
- **403** — подпись невалидна **или** `exp` истёк **или** `projects.user_id` ≠ `ownerUserId` подписи.
- **404** — проект не найден, либо файл (`path`) не найден в проекте (после нормализации). `404` (а не `403`) для
  несуществующего проекта/файла — не раскрывать существование чужих ресурсов.

### Security-заголовки (ADR-010, threat model — [05-security.md](05-security.md))
- `Content-Security-Policy: sandbox allow-scripts allow-forms; default-src 'self'; frame-ancestors 'self'`
- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: SAMEORIGIN`
- `Cache-Control: private, no-store`
- Никаких `Set-Cookie`; эндпоинт не читает/не выставляет cookies, не участвует в пользовательской сессии.

### Правила
- TTL: истёкший `exp` → `403` (проверяется вместе с HMAC, constant-time).
- Path-traversal guard: нормализация `path`, запрет `..`/абсолютных/`\`/NUL; lookup по `(project_id, normalized_path)` в
  `site_files` (не обращение к ФС). Несовпадение → `404`.
- Content-type — строго из `site_files.content_type` (allowlist), не из расширения и не из заголовков запроса.
- Изоляция: читаются только файлы `projectId` из подписи; `ownerUserId` сверяется с `projects.user_id`.
