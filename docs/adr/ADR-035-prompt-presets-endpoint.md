# ADR-035 — Пресеты промтов: статический реестр + `GET /v1/presets`

- **Статус:** Accepted (локализация — ревизия 2026-07-02 → [ADR-049](ADR-049-presets-localization.md))
- **Дата:** 2026-06-17
- **Ревизия:** §5 «Локализация — без i18n на старте» **пересмотрена** [ADR-049](ADR-049-presets-localization.md) (2026-07-02): `GET /v1/presets` локализуется (per-locale `title`/`prompt`, per-instance `PRESETS_DEFAULT_LOCALE` + `?locale=`/`Accept-Language`; `id`/`icon` не переводятся). Тело ADR-035 ниже не переписано (immutability) — актуальная локализация в [ADR-049](ADR-049-presets-localization.md).
- **Связано:** [ADR-019](ADR-019-tools-catalog-endpoint.md) (паттерн JWT-protected статического каталога — образец), [ADR-034](ADR-034-user-model-selection.md) (Поставка 1 плана model/presets; паттерн `GET /v1/models`), [ADR-001](ADR-001-stack-choice.md) (стек/модульный монолит), [ADR-033](ADR-033-llm-provider-abstraction.md) (провайдер-агностичность инстансов), [ADR-017](ADR-017-shared-server-traefik-deploy.md) (мульти-инстанс), [Q-016-1](../99-open-questions.md) (Actions/styles — клиентские пресеты), [Q-034-2](../99-open-questions.md) (presets как надстройка)

## Контекст

На главном экране чата iOS (экран 4 дизайна) — горизонтальная лента чипов-пресетов: **Plan Week, Meeting Notes, Tasks from Photo, Design Brief, Daily Review, Summarize Text, Project Structure**. Тап по чипу подставляет заранее заготовленный текст промта в композер. Сейчас этот набор зашит в клиенте; чтобы менять состав/тексты пресетов без релиза приложения, нужен серверный источник.

Это **Поставка 2** плана model/presets (Поставка 1 — выбор модели, [ADR-034](ADR-034-user-model-selection.md)). Малая аддитивная фича: нет состояния, нет биллинга, не зависит от провайдера.

Ограничения и инварианты:
- **Аддитивно, обратная совместимость не затрагивается.** Новый эндпоинт; существующие контракты/таблицы/tool-loop/биллинг не меняются. Эндпоинта `/v1/presets` сейчас нет.
- **Провайдер/инстанс-агностично** ([ADR-033](ADR-033-llm-provider-abstraction.md)): один и тот же ответ на всех 3 инстансах (broadnova/avelyra = Anthropic, orvianix = OpenAI). Пресеты — это просто тексты промтов, они не зависят от модели.
- **Без состояния и без списаний.** Чтение пресета не создаёт сессию, не пишет audit, не трогает ledger ([ADR-006](ADR-006-credit-billing-and-subscription-grant.md)).

## Решение

### 1. Контракт `GET /v1/presets`

JWT-protected (`CurrentUser`), как [`GET /v1/tools`](ADR-019-tools-catalog-endpoint.md) и [`GET /v1/models`](ADR-034-user-model-selection.md) — единый авторизационный контур `/v1/*`. Каталог пресетов **не секретен**, но эндпоинт встроен в `/v1/*` и подчиняется его сквозным правилам (gateway middleware, JWT-верификация); незачем вводить исключение для одного read-эндпоинта. Клиент к моменту запроса уже имеет JWT (получен через `/v1/auth/register`, [ADR-018](ADR-018-embedded-auth-issuer.md)) — дополнительной стоимости нет. Метод — `GET` (read-only, кэшируемо, без побочных эффектов). Per-user rate-limit как у прочих reads (`enforce_other_limits`).

**Response 200:**
```json
{
  "presets": [
    {
      "id": "plan_week",
      "title": "Plan Week",
      "icon": "calendar",
      "prompt": "Help me plan my upcoming week. Ask me about my priorities, deadlines, and commitments, then propose a balanced day-by-day schedule."
    }
  ]
}
```

Поля каждого пресета:
- `id` — **стабильный slug** (`[a-z0-9_]`, snake_case), идентификатор пресета. Стабилен между релизами; клиент может использовать для аналитики/кэша. Уникален в наборе.
- `title` — отображаемое имя чипа (как на дизайне: `"Plan Week"`).
- `icon` — строка-**имя SF Symbol** (например `"calendar"`, `"doc.text"`, `"camera"`). См. §4 «Формат иконки».
- `prompt` — текст, подставляемый в композер при тапе. Plain text, без шаблонов/плейсхолдеров на старте.

Порядок элементов — детерминированный (порядок объявления в реестре = порядок чипов на экране). Все 4 поля обязательны и непусты.

**Коды:** `200`; `401` нет/невалидный JWT; `429` rate-limit. Контракт провайдер-агностичен — идентичен на всех инстансах.

### 2. Источник пресетов — статический реестр в коде (на старте)

Пресеты задаются **статическим реестром в коде** — модуль `src/app/chat/presets.py`, по образцу `tool_catalog()` (`src/app/chat/tools.py`, [ADR-019](ADR-019-tools-catalog-endpoint.md)). Один источник истины, под версионным контролем, изменения проходят review/CI.

**Обоснование выбора (3 рассмотренных варианта):**
- **Статический реестр в коде (выбран).** Просто, версионируется, тестируемо, нулевая операционная стоимость. «Менять без релиза приложения» уже достигнуто: пресеты отдаёт backend, iOS их не хардкодит — обновление набора = деплой backend (CI/CD уже есть, [ADR-017](ADR-017-shared-server-traefik-deploy.md)), **без релиза iOS-приложения в App Store**. Это и есть целевое требование задачи.
- **Config-JSON в env (`PROMPT_PRESETS`).** Позволяет менять пресеты без пересборки образа (правка `.env` + рестарт). Отклонено на старте как преждевременное: длинные многострочные `prompt`-тексты неудобны и хрупки в env; разный набор на 3 инстансах (env per-instance, [ADR-017](ADR-017-shared-server-traefik-deploy.md)) противоречит требованию «одинаково на всех инстансах»; нет review/diff для текстов. **Зафиксирован путь миграции** на config-JSON, если понадобится правка без деплоя — [TD-026](../100-known-tech-debt.md). При переходе формат env-объекта парсится по образцу `token_products()`/`allowed_models()` (§парсинг в [config.py](../../src/app/config.py)).
- **Таблица БД (редактируемые пресеты + админ-CRUD).** Оверкилл на старте: новая таблица + миграция + admin-эндпоинты + UI ради 7 статичных строк. Отклонено; возможный пост-MVP путь, если потребуется редактирование оператором без деплоя — [Q-035-1](../99-open-questions.md).

Переход реестр → config/БД **не меняет публичный контракт** `GET /v1/presets` (источник инкапсулирован за роутером), поэтому отложен без риска для клиента.

### 3. Набор пресетов по умолчанию (7, со скрина)

Реестр наполняется бэкендом следующими 7 пресетами в этом порядке. `icon` — имена SF Symbol (§4). `prompt` — разумные дефолтные тексты (EN, §5):

| `id` | `title` | `icon` (SF Symbol) | `prompt` (текст) |
|---|---|---|---|
| `plan_week` | Plan Week | `calendar` | `Help me plan my upcoming week. Ask me about my priorities, deadlines, and commitments, then propose a balanced day-by-day schedule.` |
| `meeting_notes` | Meeting Notes | `person.2` | `Turn my raw meeting notes into a clean summary with key decisions, action items (with owners), and open questions. I'll paste the notes next.` |
| `tasks_from_photo` | Tasks from Photo | `camera` | `I'll attach a photo of a note, whiteboard, or list. Extract every actionable task from it and return them as a clear checklist.` |
| `design_brief` | Design Brief | `paintbrush` | `Help me write a concise design brief. Ask me about the goal, audience, scope, constraints, and success criteria, then draft the brief.` |
| `daily_review` | Daily Review | `checklist` | `Guide me through a short daily review: what I accomplished, what's still open, and the top 3 priorities for tomorrow.` |
| `summarize_text` | Summarize Text | `doc.text` | `Summarize the text I provide. Give a 3-sentence overview, then key points as bullets. I'll paste the text next.` |
| `project_structure` | Project Structure | `folder` | `Help me design a project structure. Ask about the project type and goals, then propose a folder/file layout with a short rationale.` |

Точные финальные формулировки `prompt` и выбор конкретного SF Symbol — за бэкендом при наполнении реестра (тексты выше — утверждённый дефолт; бэкенд может уточнить формулировку, не меняя `id`/`title`/смысл). `icon`-имена должны быть валидными SF Symbol (iOS отрисовывает их нативно).

### 4. Формат иконки — имя SF Symbol

`icon` = строка-**имя SF Symbol** (Apple), а не emoji. Обоснование:
- Клиент — нативный iOS; SF Symbols отрисовываются `Image(systemName:)` с корректным масштабированием, весом и tint под тему — единый визуальный язык с остальным приложением.
- Emoji зависят от шрифта/версии ОС, не подкрашиваются под тему, выглядят инородно в чип-ряду.
- Если клиент не находит символ по имени (опечатка/версия iOS) — рендерит fallback-иконку на своей стороне; backend не валидирует существование символа (это клиентский ресурс).

Контракт фиксирует: `icon` — непустая строка, семантика = имя SF Symbol. Смена на другой формат (например пара `{type, value}`) — несовместимое изменение, потребует нового ADR.

### 5. Локализация — без i18n на старте (EN)

На старте `title`/`prompt` — **на одном языке (английский), без локализации**. Поля `title`/`prompt` отдаются как есть, без `Accept-Language`-ветвления и без per-locale наборов. Обоснование: минимальная фича, единый набор на всех инстансах; добавление i18n сейчас усложнило бы и реестр, и контракт без подтверждённой потребности. Язык дефолтных текстов — EN (нейтрально для мультиязычной аудитории, согласуется с tool-descriptions). Локализация (по `Accept-Language` или клиентский перевод по `id`) — **открытый вопрос на будущее** [Q-035-2](../99-open-questions.md); переход аддитивен (можно добавить локализованные поля/ветвление, не ломая `id`).

### 6. Реализация (точные указания backend)

- **Реестр:** `src/app/chat/presets.py` — статический список пресетов (single source of truth) + функция `preset_catalog() -> list[dict]`, отдающая список словарей `{id, title, icon, prompt}` в порядке объявления (по образцу `tool_catalog()` в `src/app/chat/tools.py`). Реестр — модульная константа (например `_PRESETS: list[Preset]` или список dict'ов); `preset_catalog()` — pure, без I/O.
- **Схема:** `src/app/schemas/presets.py` — `PresetInfo(StrictModel)` с полями `id: str`, `title: str`, `icon: str`, `prompt: str` (все с `Field(description=...)` на русском, по образцу `schemas/tools.py`/`schemas/models.py`); `PresetsResponse(StrictModel)` с `presets: list[PresetInfo]`.
- **Роутер:** `src/app/api_gateway/routers/presets.py` (фактический каталог роутеров — `app/api_gateway/routers/`, НЕ `app/routers/`) — `APIRouter(prefix="/v1/presets", tags=["Presets"])`, эндпоинт `GET ""` по образцу `routers/tools.py`: зависимость `CurrentUser` (JWT), `enforce_other_limits(user_id=...)` → при превышении `RateLimitedError`, возврат `PresetsResponse.model_validate({"presets": preset_catalog()})`.
- **Регистрация в `main.py`:** импорт `presets` в группе `from app.api_gateway.routers import (... presets ...)` и добавление `presets` в кортеж `include_router(...)` (рядом с `tools`/`models`). Добавить тег `{"name": "Presets", "description": "Пресеты промтов для чипов на главном экране чата."}` в `_OPENAPI_TAGS`.
- **Auth:** JWT-protected, `CurrentUser` (без admin-токена, без public-исключения). Никаких новых секретов/env. Никакой записи в БД/ledger/audit.

## Альтернативы

- **Public (без JWT) `/v1/presets`.** Отклонено: ввело бы исключение в gateway-auth ради единственного read-эндпоинта; выгоды нет (клиент уже с JWT), а API-surface для анонимов растёт. Симметрично решению [ADR-019](ADR-019-tools-catalog-endpoint.md).
- **Config-JSON env `PROMPT_PRESETS` на старте.** Отклонено (§2): хрупкость многострочных текстов в env, риск рассинхрона набора между инстансами, нет review/diff. Путь миграции зафиксирован ([TD-026](../100-known-tech-debt.md)).
- **Таблица БД + admin-CRUD на старте.** Отклонено (§2): оверкилл для 7 статичных строк; пост-MVP при потребности редактирования оператором ([Q-035-1](../99-open-questions.md)).
- **`icon` как emoji.** Отклонено (§4): зависимость от шрифта/ОС, нет theming, инородно нативному iOS-UI.
- **i18n с первого дня.** Отклонено (§5): преждевременное усложнение без подтверждённой потребности; аддитивный путь оставлен ([Q-035-2](../99-open-questions.md)).
- **Пресеты как комбинация модель+режим (а не просто текст).** Вне scope этой поставки: на скрине чип подставляет **текст** в композер; пресеты модель+режим — отдельная надстройка ([Q-034-2](../99-open-questions.md)), может лечь на тот же эндпоинт аддитивными полями позже.

## Последствия

- **Положительные:** состав и тексты чипов-пресетов меняются деплоем backend **без релиза iOS-приложения**; единый набор на всех 3 инстансах (провайдер-агностично); машиночитаемый источник для UI; нулевая операционная и схемная стоимость (нет миграции, нет env, нет секретов); полностью аддитивно, обратная совместимость не затронута.
- **Цена:** новый модуль реестра (`chat/presets.py`), схема (`schemas/presets.py`), роутер (`routers/presets.py`) + регистрация в `main.py`. Правка пресетов = деплой (а не правка env/БД) — осознанный trade-off ([TD-026](../100-known-tech-debt.md)).
- **Tech debt:** редактирование пресетов без деплоя (config-JSON / БД) не реализовано ([TD-026](../100-known-tech-debt.md)); локализация отсутствует ([Q-035-2](../99-open-questions.md)).
- **Безопасность:** пресеты не секретны; JWT-контур переиспользован (без новой поверхности); read-only без побочных эффектов; тексты — статичные константы под review, не пользовательский ввод; под общими size/rate-лимитами `/v1/*`.
