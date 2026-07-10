# ADR-020 — Мультимодальный ввод: inline base64-вложения в /chat/run (MVP)

- Статус: Accepted
- Дата: 2026-06-03
- Заменяет (для MVP-пути): транспортную часть [ADR-014](ADR-014-multimodal-attachments.md) (двухшаговый upload `/v1/attachments` → ссылка). См. § «Отношение к ADR-014».
- Связан с: chat-orchestrator, [05-security.md](../05-security.md), [02-tech-stack.md](../02-tech-stack.md), [ADR-006](ADR-006-credit-billing-and-subscription-grant.md) (биллинг), [ADR-008](ADR-008-provider-tool-use-id.md) (реплей истории), [TD-009](../100-known-tech-debt.md), [TD-015](../100-known-tech-debt.md), [TD-016](../100-known-tech-debt.md), [Q-020-1](../99-open-questions.md), [Q-020-2](../99-open-questions.md).

## Context

Пользователь iOS прикрепляет фото, PDF и текстовые файлы к сообщению (дизайн: «Tasks from Photo», «Add photos», «Add files»). Их нужно передать Claude как мультимодальный ввод: изображения — vision (`image`-блок), PDF — `document`-блок, текстовые файлы — как текст. Модели `claude-sonnet-4-5` (BYOK `claude-sonnet-4-6`) поддерживают и vision, и PDF-document-блоки.

[ADR-014](ADR-014-multimodal-attachments.md) (Accepted, 2026-06-02) выбрал **двухшаговую модель** (`POST /v1/attachments` multipart → `attachmentId` → ссылка в `/chat/run`), отвергнув inline base64 из-за лимита тела `≤512KB`. Эта модель спроектирована, но **не реализована** (модуль `attachments` — статус «Спроектирован, Спринт 3»).

Точки расширения в коде (подтверждено чтением):
- `src/app/schemas/chat.py::ChatRunRequest` — `StrictModel` (`extra='forbid'`), есть `_check_sizes`; поля `attachments` сейчас **нет** (в коде; в docs-контракте оно описано как ссылочный массив `[{id}]`).
- `src/app/chat/orchestrator.py:212-217` — user-turn персистится как `chat_steps.payload = {"content":[{"type":"text","text":message}]}`.
- `orchestrator.py:377-399` (`_build_messages`) — на КАЖДОМ витке tool-loop история реконструируется из сохранённых `chat_steps.payload["content"]` **дословно** (TD-002). Любые блоки в user-turn реплеятся на всех последующих витках.
- `src/app/chat/anthropic_client.py::create_message` — передаёт `messages` как **сырые dict** (`cast(Any, messages)`), не типизированные SDK-параметры. SDK — `anthropic 0.39.0`.

**Эмпирически проверено (uv run):** `anthropic 0.39.0` экспортирует `ImageBlockParam`, но **НЕ** `DocumentBlockParam` — типизированной поддержки `document`-блока в этой версии SDK нет. Поскольку backend передаёт messages как сырые dict (а не типизированные `*BlockParam`), SDK не отвергает `{"type":"document",...}` на уровне типов — он сериализует dict как есть. Совместимость с Anthropic API определяется не SDK-версией, а самим endpoint'ом: PDF-document-блоки в Messages API доступны для актуальных моделей без beta-заголовка. Риск — в отсутствии типизации/валидации на стороне SDK (см. § Consequences, [TD-016](../100-known-tech-debt.md)).

## Decision

Для **MVP** принять **inline base64-транспорт**: вложения передаются прямо в теле `POST /v1/chat/run` в новом опциональном поле `attachments[]`. Отдельный upload-эндпоинт и таблица `attachments` (ADR-014) на MVP **не реализуются** — переносятся в [TD-015](../100-known-tech-debt.md) (storage-вариант при росте требований).

### 1. Транспорт — inline base64

```jsonc
"attachments": [
  {
    "type": "image | document | text",   // класс вложения
    "mediaType": "image/png",             // конкретный MIME из allowlist
    "filename": "photo.png",              // опц., для человекочитаемой разметки
    "data": "<base64>"                    // base64-кодированное содержимое
  }
]
```

- Поле опциональное; отсутствие = текущее поведение (обратная совместимость).
- Лимит **тела `/v1/chat/run`** поднимается отдельной настройкой (см. § Лимиты): inline base64 крупных файлов превышает общий `≤512KB`. Общий лимит прочих эндпоинтов (`≤512KB`) **не меняется** — повышенный лимит применяется только к роуту `/v1/chat/run` (transport-level, до парсинга).
- URL-вложения (`source.type=url`) **запрещены** на MVP — устраняет SSRF-вектор (backend не фетчит внешние URL). Только inline base64.

**Обоснование выбора inline для MVP:**
- Нет нового хранилища, таблицы, retention-джоба, lifecycle — меньше кода и поверхности ошибок к MVP.
- Один round-trip вместо двух (upload + run) — проще для iOS-клиента.
- Вложения в сценариях дизайна — одноразовый ввод к одному сообщению (фото задачи, PDF на разбор), не переиспользуемая библиотека → выгода двухшаговой модели (reuse, отдельный transport) для MVP не оправдывает её стоимость.
- Контроль данных и единый путь валидации в одном месте (схема + orchestrator).

### 2. Классы вложений и маппинг в Anthropic content-блоки

Allowlist (не denylist). Вне allowlist → `422 unsupported_media_type`.

| Класс (`type`) | Разрешённые `mediaType` | Anthropic content-блок |
|---|---|---|
| `image` | `image/jpeg`, `image/png`, `image/gif`, `image/webp` | `{"type":"image","source":{"type":"base64","media_type":<mediaType>,"data":<base64>}}` |
| `document` | `application/pdf` | `{"type":"document","source":{"type":"base64","media_type":"application/pdf","data":<base64>}}` |
| `text` | `text/plain`, `text/markdown`, `text/csv`, `application/json` | `{"type":"text","text":"<filename>\n```\n<декодированный текст>\n```"}` — инлайн как текст с явной разметкой имени файла |

- **Текстовые файлы → `text`-блок (не document).** Решение: текст инлайнится как `text`-блок с явным префиксом имени файла и fenced-разметкой (`filename` + содержимое в code-fence). Проще, без `document`-обёртки, детерминированно, не зависит от document-поддержки. Декодированный текст обязан быть валидным UTF-8 (иначе `422`); усечение по лимиту размера до декодирования.
- **PDF → нативный `document`-блок** (base64). НЕ извлекаем `extracted_text` на старте (ADR-014 предполагал `pypdf`-извлечение) — отдаём PDF Claude нативно; он сам разбирает страницы и изображения внутри. Это убирает `pypdf` из критического пути MVP-фичи (но см. § Лимиты — `pypdf` всё равно нужен для guard числа страниц).
- **Прочие/бинарные/неизвестные MIME** (`application/octet-stream`, `application/zip`, DOCX, HEIC и т.п.) → `422 unsupported_media_type`. Расширение allowlist — [Q-020-1](../99-open-questions.md).
- `type` в запросе и реальный `mediaType` (по magic bytes) должны быть согласованы (см. § Безопасность); рассогласование → `422`.

### 3. Реплей в tool-loop и хранение (ключевое решение)

**Проблема.** `_build_messages` реплеит user-turn дословно из `chat_steps.payload["content"]` на каждом витке. Если хранить полные base64-блоки в payload, они: (а) раздувают `chat_steps` в БД (BYTEA-эквивалент в JSON ещё и в base64 = +33%); (б) повторно отправляются Anthropic на каждом tool-раунде → лишние токены и стоимость; (в) грузятся из БД на каждый виток (TD-002).

**Решение — full-content на первом витке, lightweight-маркеры при реплее:**

1. **Персист (первый user-turn).** `chat_steps.payload["content"]` сохраняет **текстовый блок сообщения + лёгкие плейсхолдеры вложений**, НЕ полный base64. Каждое вложение сохраняется как метаданный плейсхолдер:
   ```jsonc
   {"type":"text","text":"[attachment: image/png \"photo.png\", 240KB — отправлено в первом обращении к модели]"}
   ```
   Полные base64-блоки в `chat_steps.payload` **не хранятся** (контроль раздувания БД и токенов; [TD-009](../100-known-tech-debt.md) о хранении байтов в БД не усугубляется).
2. **Первый вызов Anthropic (виток 0 message-шага).** Orchestrator собирает messages: для текущего нового user-turn использует **полные** content-блоки вложений (image/document/text), декодированные из запроса in-memory — Claude видит фото/PDF/текст один раз, в момент отправки.
3. **Последующие витки tool-loop (виток ≥1, re-entry из `/chat/tool-result`).** История реконструируется из `chat_steps.payload` → user-turn содержит **только** текстовый плейсхолдер. Тяжёлый base64-контент **не реплеится**. Модель уже «увидела» вложение на витке 0; на продолжении ей достаточно текстового упоминания факта вложения.

**Обоснование стрипа после первого витка.** Vision/PDF-контент нужен модели в момент первичного анализа; на tool-continuation (модель уже решает, какой инструмент звать) повторная передача мегабайтов base64 — чистые расходы токенов без пользы. Prompt caching Anthropic частично смягчил бы повтор, но не отправлять вовсе — дешевле и проще. Компромисс: при очень длинном tool-loop модель теряет прямой доступ к пикселям/PDF после витка 0 — приемлемо для MVP (вложения — ввод к запросу, не рабочая память на весь loop). Пересмотр — [Q-020-2](../99-open-questions.md).

**Инвариант хранения.** `chat_steps.payload` НИКОГДА не содержит сырой base64 вложений. Это держит размер шага ограниченным и совместимо с реконструкцией истории (TD-002) без раздувания.

### 4. Биллинг

Без изменений. 1 кредит = 1 сообщение ([ADR-006](ADR-006-credit-billing-and-subscription-grant.md)). Vision/document-токены входят в обычный message-шаг, отдельной тарификации вложений **нет**. Применимо к `mode=credits` и `mode=byok`. Trial — без изменений (vision-сообщение = обычный message-шаг). usage (включая возросшие inputTokens от изображений) пишется в `chat_steps.usage` для аудита, на `amount=1` не влияет.

### 5. Область применения

Вложения принимаются **только** в `POST /v1/chat/run` и **только** в первом (новом) пользовательском message-шаге. В `POST /v1/chat/tool-result` вложения **не принимаются** (схема `ChatToolResultRequest` не расширяется): tool-result — это структурированный ответ инструмента, не пользовательский мультимодальный ввод. Это устраняет неоднозначность «куда реплеить вложение в середине tool-loop».

## Consequences

- `ChatRunRequest` дополняется опциональным `attachments[]` (типизированная Pydantic-схема, `extra='forbid'`). Валидатор размеров расширяется лимитами вложений (см. § Лимиты, новые `ATTACHMENT_*` settings).
- Лимит тела `/v1/chat/run` повышается отдельной настройкой; общий `≤512KB` прочих роутов сохраняется. `SizeLimitMiddleware` должна различать лимит per-route (backend-деталь, см. [05-security.md](../05-security.md)).
- Модуль `attachments` и таблица `attachments` на MVP **не реализуются**; ADR-014 переходит в Superseded (для транспорта) — двухшаговый upload/storage становится [TD-015](../100-known-tech-debt.md).
- `pypdf` остаётся в стеке — нужен НЕ для извлечения текста, а для **guard числа страниц PDF** (анти-decompression-bomb, § Безопасность). `python-multipart` для MVP-фичи **не нужен** (нет multipart-upload) — но уже в стеке, удаление — отдельное решение (не делаем, безвредно).
- SDK `anthropic 0.39.0` не типизирует `document`-блок; backend полагается на dict-passthrough. Риск (отсутствие SDK-валидации, возможные изменения формата document-блока в будущих моделях) зафиксирован как [TD-016](../100-known-tech-debt.md) (bump SDK при необходимости). Wire-совместимость для `claude-sonnet-4-5/4-6` подтверждается отдельно backend'ом эмпирически (e2e с реальным Anthropic, см. [06-testing-strategy.md](../06-testing-strategy.md)).
- Vision/PDF увеличивают inputTokens → стоимость запроса; при `mode=credits` это не меняет списание (1 кредит), но повышает себестоимость сервисного ключа. Допустимо для MVP; мониторинг через usage в `chat_steps`.

## Отношение к ADR-014

[ADR-014](ADR-014-multimodal-attachments.md) → статус **Superseded (транспорт)**. Сохраняется как зафиксированная альтернатива/будущий путь: при появлении требований переиспользования вложений между сообщениями, очень больших файлов или object-storage — вернуться к двухшаговой модели (upload-эндпоинт + таблица + retention), см. [TD-015](../100-known-tech-debt.md). Концептуальные решения ADR-014 (allowlist, владелец=`sub`, биллинг как обычный шаг, image→image / pdf→document) **сохраняются**; меняется только транспорт (inline вместо upload) и хранение (не персистим байты).

## Alternatives

- **Двухшаговый upload (ADR-014).** Отвергнут для MVP: новое хранилище/таблица/retention/lifecycle, два round-trip, оправдан только при reuse/очень больших файлах. → [TD-015](../100-known-tech-debt.md).
- **Anthropic Files API (предзагрузка на стороне Anthropic).** Отвергнут на MVP: внешняя зависимость от файлового хранилища провайдера, отдельный lifecycle, SDK 0.39.0 не поддерживает. Опция оптимизации позже.
- **PDF через `extracted_text` (pypdf) вместо нативного document-блока.** Отвергнут: теряет верстку/изображения внутри PDF; нативный document-блок точнее. `pypdf` оставлен только для guard числа страниц.
- **Хранить полный base64 в `chat_steps.payload` и реплеить.** Отвергнут: раздувание БД (+33% от base64), повтор мегабайтов токенов на каждом tool-раунде. Принят strip-after-first-turn.
- **URL-вложения.** Отвергнуты на MVP: SSRF-вектор (backend-fetch произвольного URL). Только inline base64.
