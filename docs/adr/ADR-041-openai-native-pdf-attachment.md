# ADR-041 — Нативная поддержка PDF-вложений на OpenAI-инстансах (снятие 422), закрывает TD-023

- **Статус:** Accepted
- **Дата:** 2026-06-19
- **Тип:** implementation-ADR (расширяет существующее проектирование, без нового модуля/миграции/биллинга).
- **Расширяет:** [ADR-020](ADR-020-inline-base64-attachments-mvp.md) (inline base64-вложения, класс `document`/PDF), [ADR-033 §5](ADR-033-llm-provider-abstraction.md) (провайдер-маппинг content-блоков; снимает зафиксированное там ограничение «PDF на OpenAI → 422»).
- **Закрывает:** [TD-023](../100-known-tech-debt.md) (PDF-вложения не поддерживаются на OpenAI).
- **Связано:** [ADR-036 §4](ADR-036-workspaces-implementation.md) (паттерн извлечения текста PDF — `workspaces/text_extract.py`), [ADR-039](ADR-039-optional-message-with-attachments.md) (file-only ход), [05-security.md §Мультимодальные вложения](../05-security.md), [API-REFERENCE.md §Вложения](../API-REFERENCE.md), [TD-004a](../100-known-tech-debt.md) (CPU-guard pypdf), [Q-033-2](../99-open-questions.md) (Responses API).

## Контекст

На OpenAI-инстансах (`LLM_PROVIDER=openai`: orvianix, veltrio) вложение PDF в `POST /v1/chat/run` (`type:"document"`, `application/pdf`) отклоняется `422 unsupported_media_type: application/pdf is not supported`. Причина — провайдер-маппинг в `src/app/chat/attachments.py` (`_openai_content_block`, ветка `PROVIDER_OPENAI`) **явно** отклоняет класс `document` ([TD-023](../100-known-tech-debt.md)). TD-023 заведён в момент проектирования ADR-033, когда OpenAI Chat Completions vision не принимал PDF на вход.

Изменившийся факт (основание для пересмотра): **OpenAI поддерживает PDF на вход** для vision-моделей (gpt-4o и новее) — извлекает текст страниц и их визуальное представление; лимиты порядка ~100 страниц / 32 MB на файл. На Anthropic-инстансах PDF уже работает нативно (`document`-блок, [ADR-020 §2](ADR-020-inline-base64-attachments-mvp.md)). Цель: на OpenAI тоже принимать PDF нативно (через file-input часть OpenAI API), а не возвращать 422 — при едином контракте `/v1/chat/run` (поле `attachments[]` одинаково для обоих провайдеров).

Факты разведки кода (подтверждено чтением):
- `src/app/chat/attachments.py` — общая валидация (allowlist, size-лимит **до** decode, валидность base64, magic-bytes `%PDF-`, `_check_pdf_pages` через `pypdf` ≤ `ATTACHMENT_PDF_MAX_PAGES`=100) выполняется ДО провайдер-ветвления; затем провайдер-маппинг ([ADR-033 §5](ADR-033-llm-provider-abstraction.md)): `anthropic` → image/document(PDF) base64-блоки; `openai` → `image_url` (image), text-блок (text), **PDF → `ValidationFailedError` 422** (`_openai_content_block`, [TD-023](../100-known-tech-debt.md)).
- Лимиты: `ATTACHMENT_MAX_BYTES_DOCUMENT`=8 MB (в пределах OpenAI 32 MB — ок), `ATTACHMENT_PDF_MAX_PAGES`=100 (= лимит OpenAI). Сырой base64 НЕ персистится — `chat_steps.payload` хранит лёгкий текстовый плейсхолдер ([ADR-020 §3](ADR-020-inline-base64-attachments-mvp.md)).
- `src/app/chat/openai_client.py` — `AsyncOpenAI`, **Chat Completions** (non-streaming). `_inject_attachments` дописывает content-части (`image_url`/`text`) в последнее user-сообщение; явно отмечено, что PDF-части сюда не доходят (TD-023).
- `src/app/workspaces/text_extract.py::_extract_pdf_text` — готовый паттерн извлечения текста PDF через `pypdf` (`page.extract_text()`, объединение страниц), переиспользует валидационные примитивы `attachments.py`. Это и есть фолбэк-донор.
- OpenAI SDK в проекте: `openai 1.109.1` (`pyproject.toml`: `openai>=1.51,<2`). Эта линейка достаточно свежа для content-части `file` в Chat Completions; **точный wire-shape backend обязан сверить с установленной версией SDK** (см. §1, §6).

## Решение

### 1. Снять отклонение PDF на OpenAI — маппить PDF в OpenAI file-input

В провайдер-маппинге для `openai` класс `document` (`application/pdf`) **больше НЕ отклоняется** `422`. Вместо `raise ValidationFailedError(...)` в `_openai_content_block` (ветка `att.type == "document"`) строится **OpenAI file-input content-часть**.

**Целевой основной формат (предпочтительный) — Chat Completions content-part `file`:**
```jsonc
{
  "type": "file",
  "file": {
    "filename": "<имя файла>",
    "file_data": "data:application/pdf;base64,<base64>"
  }
}
```
- `file_data` — data-URI с MIME `application/pdf` и тем же `data` (base64), что прислал клиент (симметрично уже используемому для image `image_url.url = "data:<mediaType>;base64,<data>"`). Сырой base64 in-memory на первый вызов; не персистится (плейсхолдер, §2).
- Часть инъектируется в последнее user-сообщение тем же путём, что image/text (`OpenAIClient._inject_attachments`): content становится списком частей, PDF-`file`-часть добавляется наравне с `image_url`/text.

**Критерий выбора основного пути:** backend ОБЯЗАН сверить точный wire-shape content-части `file` с **установленной** версией `openai` SDK (`1.109.1`) и с используемым endpoint (`chat.completions.create`, non-streaming) — реальным вызовом против OpenAI (e2e на OpenAI-инстансе/ключе). Если SDK/endpoint Chat Completions принимает `file`-часть (отдаёт 200, модель видит содержимое PDF) — это финальный путь.

### 2. Фолбэк (принятый запасной путь) — серверное извлечение текста PDF

Если установленный SDK/endpoint Chat Completions **НЕ** принимает content-часть `file` (ошибка валидации SDK, 400 от OpenAI на `type:"file"`, либо часть игнорируется моделью), backend применяет фолбэк **в openai-ветке** `_openai_content_block` для `document`:

- Извлечь текст PDF на сервере через `pypdf`, **переиспользуя паттерн** `workspaces/text_extract.py::_extract_pdf_text` (тот же `pypdf`, объединение страниц `"\n\n".join(...)`, обработка `PdfReadError/ValueError/OSError` → `ValidationFailedError` «PDF could not be parsed»). Извлечение выполняется ПОСЛЕ общей валидации (включая `_check_pdf_pages`), на уже декодированных байтах (`decoded`), которые в `prepare_attachments` доступны.
- Подать извлечённый текст как **text-блок** OpenAI (как для класса `text`): `{"type":"text","text":"<filename>\n\`\`\`\n<извлечённый текст>\n\`\`\`"}` (тот же `_text_block`-формат). Имя файла — из `filename` (§3).
- Если извлечённый текст пуст (скан/image-only PDF) — отдать text-блок с пометкой об отсутствии извлекаемого текста (например `"<filename>\n[PDF без извлекаемого текста]"`); НЕ падать 422 (PDF валиден, просто не текстовый). Растеризация страниц в `image_url` на этой поставке НЕ вводится (вне scope; при потребности — отдельный ADR, [Q-033-2](../99-open-questions.md)/новый Q).

Фолбакный путь — на чистом стеке (pypdf уже в зависимостях), провайдер-агностичных частей не трогает, не требует SDK-фич.

**ADR фиксирует ОБА пути как принятые:** основной (native file-input) — предпочтительный; фолбэк (text-extraction) — гарантированный запасной. Backend выбирает основной, если sanity-check против установленного SDK/endpoint подтверждает приём `file`-части; иначе — фолбэк. Выбор фиксируется в реализации (если фолбэк — комментарий со ссылкой на этот ADR §2 + кратким обоснованием «SDK/endpoint не принял file-часть»).

### 3. Имя файла (`filename`)

- Источник — `AttachmentIn.filename` (поле уже есть в схеме, `str | None`, `max_length=512`).
- При отсутствии — дефолт `"file"` (уже используемый дефолт в `_placeholder`/`_text_block`; для PDF допустимо `"file.pdf"` — на усмотрение backend, но детерминированно). Дефолт не влияет на корректность; служит человекочитаемой меткой для модели.

### 4. Валидация и лимиты — без изменений

Существующая общая валидация и лимиты **сохраняются** и остаются в пределах OpenAI:
- magic-bytes `%PDF-`, валидность base64, size-лимит **до** decode;
- page-guard `_check_pdf_pages` ≤ `ATTACHMENT_PDF_MAX_PAGES`=100 (= лимит OpenAI ~100 стр.);
- `ATTACHMENT_MAX_BYTES_DOCUMENT`=8 MB (в пределах OpenAI 32 MB);
- password-protected/подозрительный PDF → `422` как прежде.

Никаких новых `ATTACHMENT_*` settings, миграций БД, изменения лимитов. Эти проверки уже общие (до провайдер-ветвления), меняется только ветка маппинга `openai/document`.

### 5. Storage-инвариант и Anthropic — без изменений

- **Сырой base64 НЕ персистится** ни в основном, ни в фолбэк-пути: `chat_steps.payload` хранит лёгкий текстовый плейсхолдер ([ADR-020 §3](ADR-020-inline-base64-attachments-mvp.md)); `prepare_attachments` возвращает `placeholders` как прежде. Файл-часть / извлечённый текст живут только in-memory для первого вызова OpenAI.
- На последующих tool-витках реплеится только плейсхолдер (тяжёлый контент не повторяется) — поведение ADR-020 не меняется.
- **Anthropic-ветка не трогается:** нативный `document`-блок остаётся ([ADR-020 §2](ADR-020-inline-base64-attachments-mvp.md), [ADR-033 §5](ADR-033-llm-provider-abstraction.md)). Изменение — исключительно в openai-ветке маппинга.
- **Биллинг неизменен:** 1 кредит = 1 сообщение ([ADR-006](ADR-006-credit-billing-and-subscription-grant.md)); PDF-токены входят в обычный message-шаг.

### 6. Граница изменений (нормативно для backend)

- Единственная точка правки маппинга — `src/app/chat/attachments.py::_openai_content_block`, ветка `att.type == "document"`: заменить `raise ValidationFailedError(...)` на построение file-input части (основной) ИЛИ извлечённый text-блок (фолбэк). Docstring модуля и `prepare_attachments` (упоминания «PDF → 422 для openai (TD-023)») обновить на новое поведение.
- `OpenAIClient._inject_attachments` (`openai_client.py`): file-часть инъектируется тем же механизмом, что image/text; снять/обновить комментарий «PDF parts never reach here (TD-023)». При фолбэк-пути правка `openai_client.py` не нужна (text-блок и так поддержан).
- Точный wire-shape file-части (`type:"file"`, ключи `filename`/`file_data`, форма data-URI) — **сверить с установленным `openai` SDK 1.109.1** реальным вызовом; при несоответствии перейти на фолбэк §2.

## Альтернативы

- **Оставить PDF→422 на OpenAI (статус-кво).** Отклонено: OpenAI теперь принимает PDF; единый контракт `/v1/chat/run` должен работать для PDF на обоих провайдерах; пользователи OpenAI-инстансов ожидают паритета с Anthropic.
- **Только text-extraction (без попытки native file-input).** Отклонено как единственный путь: теряет визуальную составляющую PDF (верстку/диаграммы/сканы), которую native file-input/vision OpenAI разбирает. Оставлен как гарантированный фолбэк.
- **Растеризация страниц PDF в `image_url`.** Отложено: требует новой зависимости рендеринга (pdf→image) и нетривиально по CPU/памяти; вне scope. При потребности — отдельный ADR ([Q-033-2](../99-open-questions.md)/новый Q).
- **OpenAI Files API (предзагрузка файла на сторону OpenAI) / Responses API.** Отложено ([Q-033-2](../99-open-questions.md)): внешний lifecycle файла, отход от текущего non-streaming Chat Completions-паритета. Inline data-URI в Chat Completions проще и достаточно.

## Последствия

- **Положительные:** PDF-вложения работают на OpenAI-инстансах нативно (или через гарантированный text-фолбэк); контракт `/v1/chat/run` единообразен для обоих провайдеров; [TD-023](../100-known-tech-debt.md) закрыт. Anthropic-путь и storage-инвариант не затронуты.
- **Цена:** небольшая провайдер-ветка в `attachments.py` (file-часть/фолбэк); при фолбэк-пути теряется визуальная составляющая PDF (только текст). При native-пути — рост inputTokens (PDF-страницы), но биллинг неизменен (1 кредит); себестоимость сервисного ключа растёт (как для image, [ADR-020 §Consequences](ADR-020-inline-base64-attachments-mvp.md)).
- **Tech debt:** растеризация/Responses API для богатого PDF-разбора на OpenAI — при потребности ([Q-033-2](../99-open-questions.md)). CPU-guard pypdf при фолбэк-извлечении — тот же остаточный риск [TD-004a](../100-known-tech-debt.md) (вход ≤ 8 MB, ≤ 100 стр.), новый долг не заводится.
- **Безопасность:** общая валидация вложений (allowlist/magic-bytes/лимиты/page-guard/анти-SSRF) не меняется; PDF-reject снимается, но валидность/структура PDF по-прежнему проверяются; сырой base64 не логируется и не персистится. Модель угроз вложений ([05-security.md §Мультимодальные вложения](../05-security.md)) не меняется.
