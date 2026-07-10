# Website Builder — Data Model

Дополняет [03-data-model.md](../../03-data-model.md). Две новые таблицы → всего **11** (было 9). PostgreSQL 16,
UUID v4 (`gen_random_uuid()`), timestamptz UTC, размеры — целочисленные байты.

## 10. projects
```sql
CREATE TABLE projects (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    external_project_id TEXT NOT NULL,            -- клиентский projectId из chat-сессии (chat_sessions.project_id)
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- один backend-проект на (пользователь, внешний projectId): идемпотентное разрешение проекта при site.write_file.
CREATE UNIQUE INDEX ux_projects_user_external ON projects (user_id, external_project_id);
CREATE INDEX ix_projects_user ON projects (user_id, updated_at DESC);
```
> `external_project_id` ≡ `chat_sessions.project_id` (TEXT) — клиентский идентификатор проекта в диалоге.
> `projects.id` (UUID) — внутренний идентификатор хранилища, фигурирует в signed URL превью и `site_files.project_id`.
> Изоляция владельца: `projects.user_id` сверяется и при tool-операциях (через сессию), и при превью (через подпись).

## 11. site_files
```sql
CREATE TABLE site_files (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id   UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    path         TEXT NOT NULL,                   -- нормализованный относительный путь (без ".."/абсолютных/NUL)
    content      BYTEA NOT NULL,                  -- содержимое файла (UTF-8 байты для текста / raw для бинарных)
    content_type TEXT NOT NULL,                   -- из content-type allowlist (см. 05-security)
    size         BIGINT NOT NULL CHECK (size >= 0),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- один файл на (проект, путь): site.write_file перезаписывает существующий путь (upsert).
CREATE UNIQUE INDEX ux_site_files_project_path ON site_files (project_id, path);
CREATE INDEX ix_site_files_project ON site_files (project_id);
```
> `size` дублирует `length(content)` для дешёвой проверки агрегатных лимитов проекта без чтения BYTEA. Поддерживается
> приложением консистентно с `content`. `content_type` фиксируется при записи и валидируется по allowlist — превью
> отдаёт его как есть (не угадывает по расширению).

## Инварианты
- **Изоляция владельца:** `site_files` → `projects` → `users` (FK-цепочка, `ON DELETE CASCADE`). Доступ к файлам только
  через проект; проект — только владельца. Превью сверяет `projects.user_id` с `ownerUserId` из подписи ([ADR-010](../../adr/ADR-010-backend-hosted-preview.md)).
- **Лимиты (конфигурируемо, [05-security.md](05-security.md)):** файл ≤ `PREVIEW_MAX_FILE_BYTES` (1 MB), сумма
  `site_files.size` по проекту ≤ `PREVIEW_MAX_PROJECT_BYTES` (10 MB), число файлов ≤ `PREVIEW_MAX_FILES` (200).
  Проверяются в `site.write_file` **до** вставки; превышение → tool `is_error` (не `5xx`).
- **path безопасен:** нормализованный относительный путь; запрет `..`, абсолютных путей, `\`, NUL. Валидируется при записи
  (`site.write_file`) и при чтении превью (defense-in-depth).
- **content_type ∈ allowlist** — иначе запись отклоняется (`422`/tool `is_error`).
- В `content` запрещено хранить секреты системы (контент пользовательский, но не должен содержать backend-секретов —
  обеспечивается тем, что backend никогда не пишет туда свои ключи).
- FK на `users` гарантирован lazy-provisioning ([ADR-007](../../adr/ADR-007-lazy-user-provisioning.md)): `projects.user_id`
  всегда соответствует существующей строке `users` (проект создаётся в ходе аутентифицированного chat-шага).

## Миграция
- Alembic expand-only: `CREATE TABLE projects`, `CREATE TABLE site_files` + индексы. Без изменения существующих 9 таблиц.
- `pgcrypto` уже включён ([03-data-model.md §Расширения](../../03-data-model.md#расширения-postgresql)).
