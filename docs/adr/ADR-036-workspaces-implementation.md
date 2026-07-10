# ADR-036 — Реализация Workspaces (рабочие пространства): таблицы, API, инъекция контекста, файлы-знания

- Статус: Accepted
- Дата: 2026-06-17
- Расширяет / уточняет: [ADR-013](ADR-013-workspace-projects-vs-website-builder.md) (Accepted — концепция модуля `workspaces` vs website-builder).
- Частично супершедит проектные решения прежнего модуля `workspaces` (Спринт 2): **стратегию хранения файлов-знаний** (см. §4) и **зависимость от отложенного модуля `attachments`** ([TD-015](../100-known-tech-debt.md)).
- Связан с: [ADR-012](ADR-012-assistant-mode-vs-billing-mode.md) (base assistant_mode prompt), [ADR-020](ADR-020-inline-base64-attachments-mvp.md) (inline-base64, извлечение текста), [ADR-033](ADR-033-llm-provider-abstraction.md) (провайдер-агностичная подача), [ADR-006](ADR-006-credit-billing-and-subscription-grant.md) (биллинг), [ADR-010](ADR-010-backend-hosted-preview.md)/[TD-009](../100-known-tech-debt.md) (BYTEA-хранение как у `site_files`), [Q-013-1](../99-open-questions.md) (RAG отложено).
- Поставка 3 (крупнейшая), 2 под-фазы: **3A — ядро**, **3B — файлы-знания**.

## Context

[ADR-013](ADR-013-workspace-projects-vs-website-builder.md) ввёл сущность «Project» как **рабочее пространство** (workspace): имя + описание + кастомные `instructions` (system-prompt проекта) + файлы-знания (контекст для всех чатов проекта) + группировка чатов внутри проекта. Зафиксировал терминологическое разведение с website-builder `projects` (другая сущность) и таблицы `workspace_projects`/`workspace_files` + `chat_sessions.workspace_project_id`.

Текущее состояние кода (разведка на 2026-06-17):
- `chat_sessions.workspace_project_id` **отсутствует** в `src/app/models/tables.py` (упомянут только в комментариях/документах как «Sprint-2 column»). Это **не дефект docs↔код** — документация ([modules/chats/02-api-contracts.md](../modules/chats/02-api-contracts.md), [chats/service.py](../../src/app/chats/service.py)) явно описывает `ChatListItemSchema.workspaceProjectId` как заглушку, всегда возвращающую `null` до Спринта 2.
- Таблиц `workspace_projects`/`workspace_files` нет; роутера `/v1/workspaces` нет; orchestrator `_system_prompt_for(assistant_mode)` фиксирован, инъекции workspace-контекста нет.
- Последняя миграция — `0010` (`chat_sessions.model`, [ADR-034](ADR-034-user-model-selection.md)).

**Проблема прежнего проектирования.** Прежний модуль `workspaces` (Спринт 2, bootstrap 2026-06-02) спроектировал файлы-знания как ссылки на таблицу `attachments` (двухшаговый upload, [ADR-014](ADR-014-multimodal-attachments.md)). Но `attachments` **отложена** ([TD-015](../100-known-tech-debt.md)) — chat-вложения на MVP реализованы inline-base64 без таблицы ([ADR-020](ADR-020-inline-base64-attachments-mvp.md)). Таким образом файлы-знания workspace были **заблокированы** несуществующей предпосылкой. Поставка 3 разблокирует фичу собственным хранением.

## Decision

Реализовать модуль `workspaces` целиком (ядро + файлы-знания), не завися от отложенного `attachments`. Решения по 6 пунктам, требовавшим фиксации.

### 1. API-путь: `/v1/workspaces` (внутренний модуль `workspaces`)

API-путь — **`/v1/workspaces`** (нормативно по [ADR-013 §Терминологическое разведение](ADR-013-workspace-projects-vs-website-builder.md)). Path-параметры (фактические в OpenAPI): `/{workspace_id}` и `/{workspace_id}/files/{file_id}` (snake_case в URL; в **телах** запросов/ответов id-поля — camelCase, напр. `fileId`, `workspaceProjectId`). Прозовые сокращения `{id}`/`{fileId}` в документах модуля ссылаются на эти же параметры. Клиентский UX-термин «Project» **не** выносится в URL: слово «project» в API уже занято website-builder (`chat_sessions.project_id`, `site.*`), и второй смысл в путях сломал бы разведение ADR-013. iOS отображает «Projects», обращается к `/v1/workspaces`. Поле привязки чата — `workspaceProjectId` (исторически закреплено в контракте `ChatListItemSchema`, не переименовывается).

### 2. Таблицы и привязка (под-фаза 3A)

- **`workspace_projects`** (таблица 13, [03-data-model.md §13](../03-data-model.md)): `id` (uuid PK), `user_id` (FK users, CASCADE), `name` (Text, NOT NULL), `description` (Text, nullable), `instructions` (Text, nullable — кастомный system-prompt), `created_at`/`updated_at`.
- **`chat_sessions.workspace_project_id`** (uuid, nullable, FK `workspace_projects.id` **ON DELETE SET NULL** — см. §5): привязка чата к workspace. Фиксируется при создании сессии, не меняется задним числом. **Не путать** с `chat_sessions.project_id` (Text, website-builder).
- Изоляция по пользователю: все запросы скоупятся `WHERE user_id = :sub`; чужой/отсутствующий workspace → `404` (BR-WS-1, никогда не раскрывать чужое существование).

### 3. Чат-привязка и инъекция инструкций (под-фаза 3A)

- `ChatRunRequest.workspaceProjectId: uuid | None` — **session-fixed** (как `mode`/`assistantMode`/`model`): фиксируется при создании сессии; на resume берётся из сессии, поле запроса игнорируется. При создании сессии валидируется **принадлежность workspace пользователю** (`sub`): чужой/несуществующий → `404 workspace_not_found` (изоляция; см. §RBAC). Пустое/отсутствующее поле → `null` (чат без workspace).
- `ChatListItemSchema.workspaceProjectId` становится **реальным** (из `chat_sessions.workspace_project_id`, не заглушка-null).
- Фильтр списка чатов: `GET /v1/chats?workspaceProjectId={uuid}` — «чаты проекта» (модуль chats). Без параметра — поведение неизменно.
- **Инъекция `instructions` в system-prompt** (orchestrator): при наличии `workspace_project_id` у сессии orchestrator подмешивает `workspace.instructions` в system-prompt **ПОСЛЕ** base assistant_mode prompt ([ADR-012](ADR-012-assistant-mode-vs-billing-mode.md)). Порядок: `base(assistant_mode)` → `\n\n` → `workspace.instructions` (если непусто) → (далее статичная `time.now`-инструкция уже входит в base). Пустые/`null` `instructions` → инъекции нет (system-prompt = base, prompt cache не ломается). Инъекция провайдер-агностична ([ADR-033](ADR-033-llm-provider-abstraction.md)) — это часть `system`, одинаково для Anthropic/OpenAI.
- **`instructions` подаются на КАЖДОМ ходе сессии — turn 0 И continuation (tool-loop).** `system`-prompt **не является частью истории сообщений** (передаётся отдельным параметром `system` на каждый вызов LLM), поэтому instructions необходимо переинъектировать в `system` при **каждом** обращении к модели, включая continuation-витки `/chat/tool-result`. Композиция выполняется единым helper'ом `_system_prompt_with_workspace(assistant_mode, instructions)` (orchestrator): на turn 0 instructions берутся из собранного workspace-контекста (`WorkspacesService.context_for_session` — instructions + файлы), на continuation — лёгким single-column чтением `WorkspacesService.instructions_for_session` (только `instructions`, файлы повторно не читаются и не подаются). Удалённый/чужой workspace или пустые instructions на continuation → base system-prompt без инъекции (graceful). **Файлы-знания (extracted_text/vision) — НЕ часть `system`**, они уже сохранены как content-блоки в истории сообщений после turn 0, поэтому на continuation не дублируются (см. §6).

### 4. Хранение файлов-знаний: собственная таблица `workspace_files` (BYTEA), НЕ через `attachments` (под-фаза 3B) — супершедит прежнее проектирование

- **`workspace_files`** (таблица 14, [03-data-model.md §14](../03-data-model.md)): `id` (uuid PK), `workspace_project_id` (FK `workspace_projects.id`, **ON DELETE CASCADE**), `filename` (Text, NOT NULL), `content` (BYTEA, NOT NULL — сырые байты файла), `media_type` (Text, NOT NULL — из allowlist), `size` (BIGINT, NOT NULL, `>= 0`), `extracted_text` (Text, nullable — извлечённый текст для document/text при загрузке), `created_at`/`updated_at`.
- **Хранение в БД (BYTEA)** — по образцу `site_files` ([ADR-010](ADR-010-backend-hosted-preview.md)). Это **сознательно отвергает** прежнюю привязку к таблице `attachments`: workspace-файлы — долгоживущий контекст проекта (переживают сессии, переустановку, доступны на разных устройствах), а inline-base64 ([ADR-020](ADR-020-inline-base64-attachments-mvp.md)) не персистится. Object storage — [TD-027](../100-known-tech-debt.md) (как [TD-009](../100-known-tech-debt.md) для `site_files`).
- **Транспорт загрузки — inline base64** в JSON-теле (НЕ multipart), симметрично [ADR-020](ADR-020-inline-base64-attachments-mvp.md): переиспользует существующие классы вложений (`type`/`mediaType`/`filename`/`data`), валидации и лимиты `attachments.py`. Multipart отвергнут: новый код парсинга/инфраструктура, асимметрия с уже принятым inline-путём чата.
- **Извлечение текста при загрузке** (синхронно, как `attachments.py`): для `text/*`+`application/json` — decode байтов; для `application/pdf` — `pypdf` (уже в стеке) извлекает текст постранично. Результат → `extracted_text`. Для `image/*` — `extracted_text = NULL` (подаётся как vision). PDF-парсинг под тем же CPU-guard caveat, что [TD-004a](../100-known-tech-debt.md).
- **Лимиты** (config, дефолты): `WORKSPACE_FILE_MAX_COUNT=20` (файлов на workspace), `WORKSPACE_FILE_MAX_BYTES=8_388_608` (8 MB на файл, как document-cap), `WORKSPACE_FILES_TOTAL_BYTES=33_554_432` (32 MB суммарно на workspace), allowlist `media_type` = тот же, что у chat-вложений ([Q-020-1](../99-open-questions.md): `image/jpeg|png|gif|webp`, `application/pdf`, `text/plain|markdown|csv`, `application/json`). Превышение числа/размера → `422`/`413`. allowlist вне списка → `422 unsupported_media_type`.

### 5. Удаление: каскад файлов + отвязка чатов (orphan)

- Удаление workspace (`DELETE /v1/workspaces/{id}`): `workspace_files` удаляются **CASCADE** (контекст бессмыслен без проекта); `chat_sessions.workspace_project_id` → **SET NULL** (чаты сохраняются как «чистые» чаты, история не теряется). BR-WS-5. Это симметрично website-builder (удаление проекта не уничтожает чаты).
- Удаление отдельного файла (`DELETE /v1/workspaces/{id}/files/{fileId}`): удаляет строку `workspace_files` (вместе с BYTEA). Идемпотентно: отсутствующий/чужой → `404`.

### 6. Инъекция файлов-знаний в чатах workspace (под-фаза 3B)

- При `/chat/run` в сессии с `workspace_project_id` orchestrator подмешивает файлы-знания **на первом ходе** (turn 0), наравне с inline-attachments:
  - **document/text** (`extracted_text` непуст): вставляется как **текстовый контекст** в system/первое сообщение, с человекочитаемой разметкой `[Файл проекта: {filename}]\n{extracted_text}`. Работает на **обоих** провайдерах ([ADR-033](ADR-033-llm-provider-abstraction.md)) — `extracted_text` это текст, не нативный PDF (поэтому ограничение PDF→422 на OpenAI [TD-023](../100-known-tech-debt.md) **не применяется** к workspace-файлам).
  - **image**: подаётся как **vision-блок** (механика общая с inline-attachments turn-0, провайдер-агностично через клиент: Anthropic image-блок / OpenAI `image_url` data-URI).
- **Лимит суммарного инжектируемого контекста** — `WORKSPACE_CONTEXT_MAX_CHARS` (config, дефолт `200_000` символов). Превышение → усечение `extracted_text` (порядок: по `created_at`, старые первыми, усекается хвост). RAG/семантический отбор — отложено ([Q-013-1](../99-open-questions.md)). Изображения в лимит символов не входят (ограничены числом/размером файлов §4).
- Подача **файлов-знаний** — **на первом ходе новой сессии workspace** (turn 0). На continuation tool-loop файлы повторно не подаются (они уже сохранены как content-блоки в истории сообщений). На resume существующей сессии файлы заново не инжектируются (как и attachments turn-0) — контекст уже в первых шагах истории. **Отличие от `instructions` (см. §3):** `instructions` — часть параметра `system` (не истории), поэтому переинъектируются на КАЖДОМ ходе (turn 0 И continuation); файлы — часть истории сообщений, поэтому подаются один раз (turn 0).

### 7. Биллинг (без изменений, [ADR-006](ADR-006-credit-billing-and-subscription-grant.md))

- Создание workspace, загрузка/удаление файлов — **бесплатно** (CRUD, не генерация). Ledger не пишется.
- Генерация в чате проекта — **1 кредит = 1 сообщение** как обычно ([ADR-006](ADR-006-credit-billing-and-subscription-grant.md)); инъекция instructions/файлов на amount не влияет (это контекст промта). Хранение файлов отдельно не тарифицируется (как [Q-010-4](../99-open-questions.md) для website-builder).

### 8. Пагинация списка workspace

- `GET /v1/workspaces` — **курсорная пагинация**, тот же паттерн, что `GET /v1/chats` (cursor по `(updated_at, id)`, `limit` по умолчанию 50, `nextCursor`). Дефолтный порядок — `updated_at DESC`. Симметрия с уже реализованным списком чатов; на типичном числе проектов на пользователя достаточно.

## Consequences

- Новые таблицы `workspace_projects` (§13) / `workspace_files` (§14) + колонка `chat_sessions.workspace_project_id`; миграция **`0011`** (expand-only, цепочка `0001`→`0011`).
- Новый пакет `src/app/workspaces/` (repository/service/router) + схемы; новый роутер `/v1/workspaces/*`; правка `ChatRunRequest`/`ChatResponse`-flow и system-prompt-композиции в orchestrator (новый helper `_system_prompt_with_workspace`, вызываемый и на turn 0, и на каждом continuation-витке — instructions живут в `system`, не в истории); сервисные методы `WorkspacesService.context_for_session` (turn 0: instructions + файлы) и `WorkspacesService.instructions_for_session` (continuation: только instructions, через `WorkspacesRepository.get_instructions`); реальный `workspaceProjectId` в списке чатов + фильтр.
- **Файлы-знания самодостаточны** — фича разблокирована, не ждёт `attachments`/[TD-015](../100-known-tech-debt.md). BYTEA-хранение → [TD-027](../100-known-tech-debt.md) (миграция в object storage при росте).
- `instructions`/файлы — пользовательский контент, подаётся модели: модель угроз инъекции чужого контента — [05-security.md §Workspaces](../05-security.md) (изоляция по `sub`, лимиты, нет исполнения, redaction логов).
- Прежние документы модуля `workspaces` (00-overview/01-context/02-api-contracts/03-architecture/06-rbac/07-implementation-phases/09-testing) **переписываются** под это решение (хранение `workspace_files` BYTEA вместо `attachments`).

## Alternatives

- **Файлы-знания через `attachments` (прежнее проектирование).** Отвергнуто: `attachments` отложена ([TD-015](../100-known-tech-debt.md)), фича была бы заблокирована; workspace-файлы — долгоживущий контекст, не одноразовый inline-ввод.
- **Multipart-upload файлов.** Отвергнуто: асимметрия с принятым inline-base64 ([ADR-020](ADR-020-inline-base64-attachments-mvp.md)), новый код парсинга. iOS уже умеет base64.
- **Path `/v1/projects`.** Отвергнуто: коллизия с website-builder-семантикой «project» ([ADR-013](ADR-013-workspace-projects-vs-website-builder.md)); сломало бы терминологическое разведение.
- **Каскадное удаление чатов при удалении workspace.** Отвергнуто: потеря истории пользователя; orphan (SET NULL) сохраняет чаты как «чистые».
- **RAG/векторный отбор файлов сразу.** Отвергнуто на старте ([Q-013-1](../99-open-questions.md)): простая вставка `extracted_text` с лимитом достаточна для MVP; RAG — при росте объёма.
