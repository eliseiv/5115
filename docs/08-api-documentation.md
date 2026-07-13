# 08 — API Documentation (OpenAPI / Swagger)

Сквозной стандарт оформления автогенерируемой OpenAPI-документации FastAPI (`/docs`, `/redoc`, `/openapi.json`). Цель — документация **на русском языке**, читаемая как нормальное руководство по API, с **полностью рабочей авторизацией в Swagger UI для тестировщика** (все способы auth покрыты security schemes), **лаконичными** user-facing текстами и явно описанными бизнес-блокировками.

Это convention поверх уже реализованного backend (все endpoint — см. [modules/api-gateway/02-api-contracts.md](modules/api-gateway/02-api-contracts.md)). Бизнес-контракты не меняются — меняется только их представление в OpenAPI. Реализация — `backend` в `src/app/` (app factory `create_app()`, схемы `src/app/schemas/`, роутеры `src/app/api_gateway/routers/`).

> Это не ADR: оформление документации не является значимым архитектурным решением (не меняет границы компонентов, контракты, модель данных, безопасность по существу). Единственный аспект с эффектом на поверхность атаки — отключение `/docs` в prod — зафиксирован здесь и в [05-security.md](05-security.md), отдельный ADR не требуется.

## R1. Язык

| Что | Язык |
|---|---|
| `FastAPI(description=...)` — общее описание API | русский |
| `summary` каждого endpoint | русский |
| `description` каждого endpoint | русский |
| `description` тегов (`openapi_tags`) | русский |
| Описания полей схем (`Field(description=...)`) | русский |
| Описания response-моделей и примеров | русский |
| Сообщения об ошибках валидации (`detail`) | как есть (генерит FastAPI/Pydantic) |

Остаются **в оригинале** (не переводятся): имена endpoint-путей (`/v1/chat/run`), имена полей схем (`sessionId`, `blockReason`), enum-значения (`assistant_message`, `credits`, `trial_used`), коды ошибок (`validation_error`, `unauthorized`), имена tools (`site.write_file`), HTTP-методы и коды, имена заголовков (`Authorization`, `X-Device-Id`, `X-Request-Id`).

Правило: **описываем по-русски, идентифицируем по-английски**. Описание поля `sessionId` — на русском («Идентификатор сессии…»), само имя поля — `sessionId`.

## R2. Security schemes (покрывают ВСЕ способы auth)

Swagger UI должен позволять тестировщику авторизоваться **любым** способом, которым реально защищён endpoint. Для этого в OpenAPI объявляются **две** security schemes, и **каждый** endpoint помечается корректной (`bearerAuth` / `adminToken` / none). Кнопка **Authorize** в Swagger UI показывает оба варианта.

### R2.1. `bearerAuth` — пользовательская auth (JWT)
- HTTP Bearer scheme c `bearerFormat: JWT` (механизм FastAPI — `fastapi.security.HTTPBearer`, имя scheme в схеме — `bearerAuth`; объявлено в `src/app/api_gateway/openapi_security.py`).
- Auth-модель — JWT **RS256**, claim `sub` = userId; см. [05-security.md](05-security.md). В `description` scheme кратко (по-русски): «JWT (RS256). В claim `sub` — userId; `userId` в теле запроса обязан совпадать с `sub`, иначе `403`. Введите `Bearer <JWT>` через Authorize — применится ко всем защищённым `/v1/*`».
- **Требуется** для пользовательских `/v1/*`: `chat` (`/v1/chat/run`, `/v1/chat/tool-result`), `GET /v1/tools` ([контракт](modules/api-gateway/02-api-contracts.md)), `policy`, `wallet`, `subscription`, `byok`, `chats`, `profile`, `preferences`, `workspaces`, `snippets`, `attachments`, `tokens`, `notifications`. У них в Swagger UI значок замка.

### R2.2. `adminToken` — admin-auth (X-Admin-Token)
- apiKey scheme: `type: apiKey`, `in: header`, `name: X-Admin-Token`, `scheme_name = adminToken`. Изолированный admin-секрет ([контракт admin](modules/admin/02-api-contracts.md), [05-security.md](05-security.md)).
- В `description` scheme кратко (по-русски): «Изолированный admin-токен. Вставьте секрет в заголовок `X-Admin-Token` через Authorize. Пользовательский JWT admin-действия не авторизует».
- **Требуется** для **всех** `/v1/admin/*` (`POST /v1/admin/wallet/grant`, `GET /v1/admin/wallet/{userId}`). До этой фичи admin-эндпоинты не имели объявленной scheme в OpenAPI → тестировщик не мог авторизоваться в Swagger UI; теперь у них значок замка `adminToken`.
- Механизм объявления — добавить scheme в OpenAPI-кастомизацию (рядом с `bearerAuth` в `src/app/api_gateway/openapi_security.py` или в `custom_openapi()`); реальная проверка остаётся в `require_admin`.

### R2.3. Публичные endpoint (без security, без замка)
- Служебные: `GET /health`, `GET /healthz` (алиас `/health`, [ADR-017](adr/ADR-017-shared-server-traefik-deploy.md)), `GET /ready`, `GET /metrics` (защищён сетью/scrape-токеном, не входит в Swagger Authorize — см. [07-deployment.md](07-deployment.md#health--readiness)).
- **Auth-issuer:** `POST /v1/auth/register`, `POST /v1/auth/token`, `POST /v1/auth/refresh`, `GET /v1/auth/jwks` — точка получения токена, защищены per-IP rate-limit ([контракт](modules/api-gateway/02-api-contracts.md)). Без security в OpenAPI — тестировщик вызывает их без авторизации, чтобы получить токен.
- Preview: `GET /v1/preview/*` — авторизуются signed URL, не JWT. Без security scheme.

### R2.4. Общее
- Объявление security scheme в OpenAPI **не подменяет** реальную проверку (JWT — в `app/api_gateway/auth.py` / `deps.py`; admin — `require_admin`). Это только описание для клиента и Swagger UI. Источник истины аутентификации не меняется.
- Каждый endpoint обязан иметь **ровно одну** корректную привязку: `bearerAuth`, либо `adminToken`, либо none (public). Привязки не смешиваются (admin-эндпоинты не принимают `bearerAuth`, и наоборот).

## R2bis. Как тестировать через Swagger (флоу тестировщика)

Swagger UI обязан быть самодостаточным для ручного тестирования всех эндпоинтов — без внешних инструментов и без ручной выпечки токенов. Зафиксированный флоу:

**Пользовательские эндпоинты (`bearerAuth`):**
1. Открыть `/docs`, раздел `Auth`.
2. Вызвать `POST /v1/auth/register` (или `POST /v1/auth/token`) прямо в Swagger UI — без авторизации (public). Скопировать `accessToken` из ответа.
3. Нажать **Authorize** → выбрать `bearerAuth` → вставить токен (как `Bearer <accessToken>` либо просто `<accessToken>` — в зависимости от поля HTTPBearer).
4. Тестировать любые `/v1/*` (chat, wallet, chats, profile, preferences, byok, subscription, tokens, `/v1/tools` и др.) — замок закрыт, токен подставляется автоматически.
5. При истечении — `POST /v1/auth/refresh`, повторить Authorize новым токеном.

**Admin-эндпоинты (`adminToken`):**
1. Нажать **Authorize** → выбрать `adminToken` → вставить значение `ADMIN_API_SECRET` (его получает тестировщик из секрет-менеджера/env, не из API).
2. Тестировать `/v1/admin/*` — заголовок `X-Admin-Token` подставляется автоматически.

**Acceptance флоу:** тестировщик проходит весь путь register → Authorize(bearerAuth) → защищённый вызов **и** Authorize(adminToken) → admin-вызов, не покидая Swagger UI. Если хотя бы одна группа эндпоинтов не авторизуется через Authorize — нарушение R2.

## R2ter. Лаконичность user-facing текстов (для тестировщиков)

OpenAPI-тексты (`summary`, `description` эндпоинтов, `Field(description=...)` в схемах) пишутся **лаконично и для тестировщика**, а не как внутренняя архитектурная документация.

**Правила:**
- `summary` — **одна строка**: что делает endpoint (повелительно/назывательно, ≤ ~60 символов). Например: «Покупка пакета токенов», «Сохранить свой ключ Anthropic».
- `description` — **только существенное для тестировщика**: что отправить, что вернётся, ключевые коды/состояния. Без пересказа внутренней механики.
- **Запрещено** в user-facing OpenAPI-текстах:
  - ссылки на ADR (например `(ADR-015)`, `(ADR-002)`), на `Q-NNN-N`, на `TD-NNN`;
  - избыточные скобки-пояснения и расшифровки аббревиатур ради аббревиатуры (например `(Bring Your Own Key)`);
  - многословные описания серверной механики (детальный маппинг, «отдельный путь от подписки», внутренние сервисы/таблицы).
- **Технические нюансы** (идемпотентность, redaction и т.п.) — упоминаются **кратко** одной фразой или показываются в примере (R5), без перегруза основного текста.
- **ADR/Q/TD-ссылки остаются ТОЛЬКО в `docs/`** (модульные контракты, ADR) — это источник истины для разработчиков. В OpenAPI их быть не должно.

**Примеры приведения к стилю (обязательны к исправлению backend):**

| Где | БЫЛО (многословно) | СТАЛО (лаконично) |
|---|---|---|
| `POST /v1/tokens/purchase` description | «Подписанная consumable-транзакция верифицируется и идемпотентно начисляет кредиты по серверному маппингу productId → credits; отдельный путь от подписки (ADR-015).» | «Покупка пакета токенов через StoreKit. Начисляет кредиты по `productId`. Повторная отправка той же транзакции не начисляет повторно.» |
| Тег / summary `BYOK` | «Свой ключ Anthropic (Bring Your Own Key)» | «Свой ключ Anthropic» |

Правило применяется и к новым эндпоинтам (`/v1/auth/*`, `GET /v1/tools`): их summary/description — в этом же лаконичном стиле.

## R3. Бизнес-блокировки (status=blocked, HTTP 200)

Документация обязана явно объяснить нестандартное правило [ADR-004](adr/ADR-004-blocked-http-200.md): бизнес-блокировка — это **успешный** ответ `200 OK` с телом `{status:"blocked", blockReason}`, а не 4xx.

- В `description` endpoint `/v1/chat/run` и `/v1/chat/tool-result` — абзац на русском: «Блокировка по бизнес-правилам возвращается с HTTP 200 и полем `blockReason` (машиночитаемо). Технические ошибки — 4xx/5xx (см. таблицу кодов)».
- В общем `description` API (R6) — короткая ссылка на это правило, чтобы интегратор не искал 4xx там, где приходит 200.
- Поле `blockReason` (в `ChatResponse`; в `reasons[]` `/policy/effective` — подмножество policy-причин, без `rate_limited`/`max_tokens`/`image_credits_empty`) описать как enum с расшифровкой **каждого** из 10 значений: что означает и что должен сделать UI. Канонический источник значений — [ADR-004](adr/ADR-004-blocked-http-200.md) (расширен `max_tokens` в [ADR-025](adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md), `image_credits_empty` в [ADR-058](adr/ADR-058-image-generation.md)); документация не вводит новых значений.

### Расшифровка blockReason (для описаний полей и общего раздела)

| Значение | Что означает | Что делает UI |
|---|---|---|
| `trial_used` | Бесплатная пробная генерация уже использована, подписки нет. | Предложить оформить подписку. |
| `subscription_required` | Действие требует активной подписки, её нет. | Экран оформления подписки. |
| `subscription_expired` | Подписка была, но истекла/отозвана. | Предложить продлить подписку. |
| `credits_empty` | Баланс кредитов исчерпан (режим `credits`). | Показать баланс, предложить пополнение/подписку. |
| `byok_disabled` | Режим `byok` выбран, но BYOK выключен пользователем. | Включить BYOK в настройках. |
| `byok_invalid` | Ключ BYOK отсутствует или невалиден (`keyStatus=invalid`, либо ключ не задан при `mode=byok` и активной подписке — `byok=missing`). | Добавить/исправить ключ в настройках. |
| `rate_limited` | Транспортное превышение rate limit; всегда возвращается как HTTP `429` (gateway-concern). НЕ приходит как `status=blocked` body и НЕ входит в `/policy/effective.reasons[]`. Значение enum сохранено только для HTTP-слоя (см. [ADR-004](adr/ADR-004-blocked-http-200.md), BLK-7b в [09-e2e-testing.md](09-e2e-testing.md)). | Показать «слишком часто», предложить повторить позже. |
| `policy_denied` | Общий fallback для непредвиденного состояния Policy Engine. | Generic-сообщение «недоступно», лог/ретрай. |

### Дискриминация ответа `/chat/run` и `/chat/tool-result`

Ответ — одна модель `ChatResponse` (`src/app/schemas/chat.py`) с тремя взаимоисключающими состояниями по полю `status`. Документация обязана сделать варианты очевидными:
- `status=assistant_message`: присутствуют `assistantMessage`, `usage`, `messageStepId`, `stepId`; нет `toolCall`/`toolCalls`, `blockReason`.
- `status=tool_call`: присутствуют **`toolCalls[]`** (все client-side tool_use хода) и `toolCall` (= `toolCalls[0]`, deprecated, [ADR-025](adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)), `usage`, `messageStepId`, `stepId`; нет `blockReason`. **`assistantMessage` — опционально присутствует**, если Claude выдал текст вместе с `tool_use` (текст того же assistant-шага `stepId`); `null`/опущено, если текста не было ([Q-024-1](99-open-questions.md) / [ADR-024](adr/ADR-024-history-payload-domain-normalization.md)).
- `status=blocked` (**policy**, `blockReason ≠ max_tokens`): присутствует `blockReason`; `messageStepId`/`stepId` = `null`; нет `assistantMessage`, `toolCall`/`toolCalls`, `usage`.
- `status=blocked` + **`blockReason=max_tokens`** (обрезка, [ADR-025](adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)): присутствуют `usage`, `messageStepId`, `stepId` (НЕ null), опционально `assistantMessage` (частичный текст); нет `toolCall`/`toolCalls` (обрезанные tool_use не отдаются). Кредит не списан.

`messageStepId`/`stepId` ([ADR-023](adr/ADR-023-sync-ids-in-chat-response.md), nullable) — идентификаторы синхронизации с историей чата: дословно совпадают с `steps[].messageStepId` / `steps[].id` из `GET /v1/chats/{id}` (модуль chats). В `description` поля `stepId` указать: «id шага этого ответа, совпадает со `steps[].id` в истории»; `messageStepId`: «ключ хода (стабилен через tool-loop), совпадает со `steps[].messageStepId`». При `blocked` оба `null` (шаг/ход не создаются — блок до генерации).

Способ (на усмотрение `backend`, любой даёт читаемый результат): либо описать инвариант в `description` модели + три именованных примера `openapi_examples` (`assistant_message`, `tool_call`, `blocked`), либо ввести discriminated-union response с тремя под-моделями. **Обязательный минимум** — три именованных примера ответа на каждый из двух chat-endpoint; примеры `assistant_message`/`tool_call` должны нести непустые `messageStepId`/`stepId`, пример `blocked` — `null`. Пример `tool_call` рекомендуется показать с непустым `assistantMessage` (Claude сказал текст + вызвал инструмент, [Q-024-1](99-open-questions.md)/[ADR-024](adr/ADR-024-history-payload-domain-normalization.md)); вариант без текста (`assistantMessage` отсутствует/`null`) — также валиден. Менять wire-формат существующих полей (имена/типы) запрещено; `messageStepId`/`stepId` — **аддитивные** nullable-поля (обратносовместимо).

## R4. Теги и группировка

Сгруппировать endpoint по модулям через `tags` + объявить порядок и русские описания в `openapi_tags`. Порядок тегов = порядок пользовательского сценария.

| Тег | Endpoint | Security | Описание тега (русский, кратко) |
|---|---|---|---|
| `Auth` | `POST /v1/auth/register`, `POST /v1/auth/token`, `POST /v1/auth/refresh`, `GET /v1/auth/jwks` | none | Получение и обновление токена доступа. Точка входа для тестирования. |
| `Chat` | `POST /v1/chat/run`, `POST /v1/chat/tool-result` | `bearerAuth` | Диалог с ассистентом и tool-loop (вызовы инструментов на устройстве). |
| `Tools` | `GET /v1/tools` | `bearerAuth` | Каталог инструментов, доступных в tool-loop. |
| `Models` | `GET /v1/models` | `bearerAuth` | Каталог доступных моделей активного провайдера для селектора модели ([ADR-034](adr/ADR-034-user-model-selection.md)). |
| `Presets` | `GET /v1/presets` | `bearerAuth` | Пресеты промтов для чипов на главном экране чата ([ADR-035](adr/ADR-035-prompt-presets-endpoint.md)). |
| `Policy` | `GET /v1/policy/effective` | `bearerAuth` | Эффективные права пользователя для UI (можно ли генерировать и почему нет). |
| `Wallet` | `GET /v1/wallet`, `POST /v1/wallet/consume` | `bearerAuth` | Баланс кредитов и списание (1 кредит = 1 сообщение). |
| `Subscription` | `POST /v1/subscription/sync` | `bearerAuth` | Синхронизация подписки StoreKit и начисление кредитов периода. |
| `Tokens` | `POST /v1/tokens/purchase`, `GET /v1/tokens/products` | `bearerAuth` | Покупка пакетов токенов и каталог продуктов. |
| `BYOK` | `POST /v1/byok/set`, `POST /v1/byok/toggle`, `POST /v1/byok/delete` | `bearerAuth` | Свой ключ Anthropic: сохранение, включение, удаление. |
| `Admin` | `POST /v1/admin/wallet/grant`, `GET /v1/admin/wallet/{userId}` | `adminToken` | Операторские действия: начисление и просмотр кошелька. Авторизация — `X-Admin-Token`. |
| `Preview` | `GET /v1/preview/{projectId}/{token}/{path}` | none | Публичная отдача сгенерированных сайтов по подписанной ссылке (авторизация в подписи, без JWT, [ADR-010](adr/ADR-010-backend-hosted-preview.md)). |
| `Chats` | `GET/PATCH/DELETE /v1/chats[/{id}]` (+ `/{id}/steps`) | `bearerAuth` | История чатов: список, переименование, удаление, шаги. |
| `Profile` | `GET/PATCH /v1/profile` | `bearerAuth` | Профиль пользователя. |
| `Preferences` | `GET/PATCH /v1/preferences` | `bearerAuth` | Пользовательские настройки. |
| `Health` | `GET /health`, `GET /healthz`, `GET /ready`, `GET /metrics` | none | Служебные проверки и метрики (без auth). `/healthz` — алиас `/health` ([ADR-017](adr/ADR-017-shared-server-traefik-deploy.md)). |

Каждый endpoint имеет ровно один тег и security согласно принципу R2. **Порядок тегов — единственный источник истины `_OPENAPI_TAGS` (`src/app/main.py`); verification-критерий — тест `test_api_documentation.py::test_tag_order`.** Документ **намеренно не дублирует** точный список/порядок тегов: захардкоженный перечень по определению отстаёт от кода — он уже **дважды** отставал (`Workspaces`, затем `Images`). Это тот же класс проблемы, что жёсткий список миграций, заменённый формулировкой «до head»: конкретику держат код + тест, документ описывает **принцип**.

**Принцип группировки `_OPENAPI_TAGS`** (по нему упорядочены теги; точный порядок — в коде): (1) точка входа и основной чат-флоу — `Auth` → `Chat` → каталоги `Tools`/`Models`/`Presets`; (2) доступ и биллинг — `Policy`/`Wallet`/`Subscription`/`Tokens`/`BYOK`; (3) операторское и публичное — `Admin`/`Preview`; (4) история и модули-расширения — `Chats`/`Images`/`Workspaces`/`Profile`/`Preferences`; (5) служебное `Health` — **последним**. Новый endpoint получает тег по этому принципу: пользовательский JWT (`bearerAuth`) для `/v1/*`, лаконичные тексты (R2ter), один тег на endpoint.

> **При добавлении/переименовании тега** правятся `_OPENAPI_TAGS` (`src/app/main.py`) **и** тест `test_tag_order` — **этот документ править не нужно** (порядок не хардкодится). `Images` (ADR-058) и `Workspaces` (ADR-036) — уже отдаваемые теги; `snippets`/`attachments`/`notifications` — будущие модули-расширения ([карта маршрутов](modules/api-gateway/02-api-contracts.md)), получат теги по тому же принципу.

## R5. Примеры (request/response)

Для ключевых endpoint — осмысленные примеры на русском (значения-плейсхолдеры реалистичны: UUID-подобные id, осмысленный текст сообщения по-русски). Минимум:

| Endpoint | Обязательные примеры |
|---|---|
| `POST /v1/chat/run` | request (`mode=credits`); response `assistant_message`; response `blocked` (`credits_empty`); response `blocked` (`max_tokens`, [ADR-025](adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md) — с `usage`/`stepId`, без `toolCalls`). Пример `tool_call` с `toolCalls[]` **убран** — client-side инструментов больше нет ([ADR-063](adr/ADR-063-remove-client-side-calendar-reminders-files-tools.md)); поле `toolCalls[]`/статус `tool_call` сохранены в схеме, но канонического примера с реальным инструментом нет. |
| `POST /v1/chat/tool-result` | request батч `results[]` (форма батча/`error` сохраняются в схеме для обратной совместимости, но в штатном потоке не порождаются — нет client-side инструментов, [ADR-063](adr/ADR-063-remove-client-side-calendar-reminders-files-tools.md)); response `assistant_message`. |
| `POST /v1/wallet/consume` | request (`amount=1`, `requestId`); response (`newBalance`, `ledgerTxId`). |
| `POST /v1/byok/set` | request (`apiKey` — плейсхолдер, помечен «не логируется»); response `keyStatus=valid` и `keyStatus=invalid`. |

Tool-loop сценарий описать связно (в `description` тега `Chat` или endpoint `/chat/run`): `run` → `tool_call` (`toolCalls[]`) → клиент исполняет **все** tool → `tool-result` (батч `results[]`) → `assistant_message`. Использовать согласованные id между примерами `toolCalls[].id` и `tool-result.results[].toolCallId`, чтобы сценарий читался end-to-end. **Parallel tool use ([ADR-025](adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)):** пример `tool_call` рекомендуется показать с ≥2 элементами `toolCalls[]` и соответствующим батч `tool-result` (барьер хода: backend продолжает только когда собраны все результаты). Поле `toolCall` (одиночное) — deprecated, помечать в `description` как «= `toolCalls[0]`, читайте `toolCalls`».

Запрещено в примерах: реальные секреты, реальные JWT, реальные ключи Anthropic, реальные StoreKit payload. Только очевидные плейсхолдеры. Для `apiKey`, `transaction`, `Authorization` — плейсхолдер + пометка о redaction (R7 [05-security.md](05-security.md)).

## R6. Метаданные API

В `create_app()` (`src/app/main.py`) при создании `FastAPI(...)` задать:

| Параметр | Значение |
|---|---|
| `title` | `claude-ios-backend` (без изменений). |
| `version` | текущая версия приложения (на момент фичи `0.1.0`; источник версии не меняется). |
| `description` | русский multiline-текст: назначение сервиса (backend-оркестратор Claude для iOS-приложения), кратко бизнес-правила доступа (trial → подписка/кредиты → BYOK), правило blocked=HTTP 200 (R3) с отсылкой к перечню `blockReason`, требование JWT (R2). Без раскрытия секретов и внутренних деталей реализации. |
| `contact` / `license` / `terms_of_service` | опционально, на усмотрение `backend`. Если заданы — без выдуманных URL/email; иначе не задавать. |

`description` должен дать интегратору контекст за один экран: что это, как авторизоваться, как читать `blocked`.

## R7. Доступность /docs в prod (env-флаг)

Документационные endpoint должны отключаться в production.

- Новая env-переменная **`DOCS_ENABLED`** (bool). Дефолт — `true` (удобно для dev/CI/staging).
- При `DOCS_ENABLED=false`: `FastAPI(docs_url=None, redoc_url=None, openapi_url=None)` — `/docs`, `/redoc`, `/openapi.json` возвращают `404`.
- В prod значение задаётся через секрет-менеджер/env согласно [07-deployment.md](07-deployment.md#конфигурация-env). Рекомендация prod — `false` (схему API не раскрывать публично).
- Флаг добавляется в `Settings` (`src/app/config.py`, alias `DOCS_ENABLED`) рядом с прочими тумблерами; `create_app()` читает его при инициализации.
- Поведение зафиксировано как security-мера (снижение раскрытия API surface) — см. [05-security.md](05-security.md#документация-api-в-prod).

## R8. Читаемость (acceptance)

Документация считается «удобной для чтения и тестирования», если:
- Каждый endpoint имеет короткий `summary` (одна строка, ≤ ~60 символов) и лаконичный `description` (R2ter): что отправить, что вернётся, ключевые коды; без ADR/Q/TD-ссылок и без избыточных скобок-пояснений.
- В user-facing OpenAPI-текстах нет вхождений `ADR-`, `Q-`, `TD-` и расшифровок-аббревиатур в скобках (например `(Bring Your Own Key)`).
- Объявлены **обе** security schemes: `bearerAuth` (JWT) и `adminToken` (`X-Admin-Token`); каждый endpoint помечен корректно (R2).
- У пользовательских `/v1/*` виден замок `bearerAuth`, у `/v1/admin/*` — замок `adminToken`, у `Auth`/`Health`/`preview` — замка нет.
- Тестировщик проходит флоу R2bis целиком в Swagger UI: register → Authorize(bearerAuth) → защищённый вызов; Authorize(adminToken) → admin-вызов.
- Endpoint сгруппированы тегами в порядке сценария (R4); внутри Swagger UI читаются как разделы руководства.
- `blockReason` раскрыт (R3): интегратор понимает каждое значение без чтения исходников.
- В ключевых endpoint есть примеры request/response (R5), включая tool-loop и blocked.
- `description` API (R6) даёт стартовый контекст.

## Scope / Out-of-scope

**В scope:**
- `src/app/api_gateway/openapi_security.py` (или `custom_openapi()` в `src/app/main.py`): **добавить вторую security scheme `adminToken`** — `apiKey`, `in: header`, `name: X-Admin-Token` — рядом с существующей `bearerAuth` (R2.2). Пометить `/v1/admin/*` этой scheme; пользовательские `/v1/*` — `bearerAuth`; `/v1/auth/*`, `/v1/preview/*`, `Health` — без security (R2.3).
- `src/app/api_gateway/routers/*.py` — **переписать `summary`/`description` во ВСЕХ роутерах** в лаконичный стиль (R2ter): убрать ADR/Q/TD-ссылки и избыточные скобки-пояснения. Прицельно: `token_purchase.py` (многословие token-purchase, убрать `(ADR-015)`), `byok.py` (убрать `(Bring Your Own Key)`). Проставить корректные `tags`/`security` (R4). Новые роутеры `auth` (public) и `GET /v1/tools` (`bearerAuth`) — в том же стиле и security.
- `src/app/schemas/*.py` — `Field(description=...)`: лаконично, убрать ADR/Q/TD-ссылки и расшифровки-аббревиатуры в скобках.
- `src/app/main.py` — метаданные API (R6), теги (R4), docs-флаг (R7); `src/app/config.py` — `DOCS_ENABLED` (R7).
- `src/app/api_gateway/routers/health.py` — служебные без security.

**Out-of-scope:** изменение wire-формата (имена/типы полей, пути, методы, коды, состав security-механизмов) — запрещено, иначе ломается контракт [modules/api-gateway/02-api-contracts.md](modules/api-gateway/02-api-contracts.md). Меняются только **тексты** (`summary`/`description`/`Field.description`) и **объявление** security schemes в OpenAPI, не сама проверка auth, бизнес-логика, rate limit. Новые endpoint не вводятся; технические идентификаторы не переводятся.

## Открытые вопросы
Нет. Все решения зафиксированы выше; дефолты явны.
