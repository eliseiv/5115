# ADR-013 — Workspace-проекты (рабочие пространства чатов) vs website-builder projects

- Статус: Accepted
- Дата: 2026-06-02
- Связан с: ADR-011 (server-side tools), website-builder, модули `workspaces`, `chats`.

## Context
Дизайн вводит сущность **«Project»** как **рабочее пространство**: имя + описание + кастомные инструкции (system-prompt, «Use a professional tone…») + прикреплённые файлы-контекст (например PDF, подаваемый Claude) + список чатов этого проекта.

В backend уже есть таблица `projects` — но это **website-builder** (хранилище сгенерированных Claude статических сайтов: `projects` + `site_files`, привязка по `external_project_id` = `chat_sessions.project_id`, server-side tools `site.*`, ADR-010/011). Это **другая** сущность с тем же словом «project».

Совмещать их нельзя: website-builder `projects` — это вывод (артефакт генерации), а дизайн-«Project» — это вход/контейнер (рабочее пространство + контекст для Claude). У них разные жизненные циклы, владение, контракты.

## Decision
Ввести **отдельный модуль `workspaces`** с собственными таблицами, не переиспользуя website-builder:

- `workspace_projects` — рабочее пространство: `id`, `user_id`, `name`, `description`, `instructions` (кастомный system-prompt, TEXT, nullable), таймстемпы.
- `workspace_files` — прикреплённые файлы-контекст: `id`, `workspace_project_id`, `filename`, `media_type`, `size`, `extracted_text` (nullable; для PDF/текста — извлечённый текст, подаваемый модели как контекст), таймстемпы.
  > **Уточнено [ADR-036 §4](ADR-036-workspaces-implementation.md) (Поставка 3, супершедит этот пункт):** хранение байтов файла переведено на **собственный BYTEA-столбец `workspace_files.content`** (образец `site_files`), а не `content_ref` на общую таблицу `attachments` ([ADR-014](ADR-014-multimodal-attachments.md)/[TD-015](../100-known-tech-debt.md), отложена). Концепция модуля (этот ADR) неизменна; меняется только реализация хранилища файлов.
- Привязка чата к проекту: `chat_sessions.workspace_project_id` (nullable FK). Чат может не принадлежать ни одному workspace (дефолтное пространство).

**Терминологическое разведение (нормативно):**
- `workspace_projects` / «workspace» — рабочее пространство чатов (этот ADR). В iOS-контракте — `/v1/workspaces`.
- `projects` (website-builder) — хранилище сгенерированных сайтов (ADR-010/011), фигурирует только внутри `site.*` и preview. Наружу в новом API website-builder-`projects` не выставляется под именем «project» в контексте workspaces.
- `chat_sessions.project_id` (TEXT) — **существующее** клиентское поле website-builder (= `projects.external_project_id`). НЕ переименовывается, НЕ путать с `workspace_project_id` (UUID FK). Это два разных поля сессии с разной семантикой.

### Подача workspace-файлов Claude
- `instructions` workspace добавляются к base-system-prompt оркестратором при генерации в сессии этого workspace (после base assistant_mode prompt, ADR-012).
- `workspace_files` подаются Claude как **контекст**: для текстовых/PDF — через `extracted_text` (backend извлекает текст при загрузке), вставляемый в prompt; для изображений — как vision-вложения (механика общая с ADR-014 мультимодальность). Точная стратегия инъекции (полный текст vs усечение/RAG) на старте — простая вставка `extracted_text` с лимитом; рост → [Q-013-1](../99-open-questions.md).

## Consequences
- Новый модуль `workspaces`, новые таблицы `workspace_projects` / `workspace_files`, колонка `chat_sessions.workspace_project_id`.
- website-builder `projects`/`site_files` и ADR-010/011 **не затрагиваются**.
- Хранилище байтов файлов workspace переиспользует подсистему вложений (ADR-014), не дублирует BYTEA-логику.
- Документация обязана различать «workspace» (рабочее пространство) и website-builder «project» явно.

## Alternatives
- **Переиспользовать website-builder `projects`.** Отвергнуто: разная семантика, владение и контракты; смешение сломало бы изоляцию ADR-010 и привязку `external_project_id`.
- **Хранить инструкции/файлы только на клиенте.** Отвергнуто: workspace и его контекст должны переживать переустановку и быть доступны на разных устройствах; это серверная сущность.
