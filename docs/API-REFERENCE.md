# API Reference — backend для iOS-приложения (Claude orchestration)

Сводный справочник по всем эндпоинтам сервиса для product-менеджера и интеграторов iOS.
Документ человекочитаемый: метод, путь, назначение, заголовки, тело запроса/ответа, коды ответа.
Внутренние архитектурные обоснования вынесены в ADR (`docs/adr/`) и здесь не дублируются — при необходимости даны ссылки.

База: все бизнес-эндпоинты под префиксом `/v1`. Транспорт — только HTTPS. Формат тела — JSON (`Content-Type: application/json`).

---

## Оглавление

1. [Базовые принципы](#1-базовые-принципы)
2. [Аутентификация и заголовки](#2-аутентификация-и-заголовки)
3. [Коды ответа (общие)](#3-коды-ответа-общие)
4. Эндпоинты по модулям:
   - [Auth](#21-auth-выпуск-токена) · [Chat](#4-chat) · [Tools](#22-tools-каталог-инструментов) · [Models](#24-models-список-моделей-инстанса) · [Presets](#25-presets-пресеты-промтов) · [Policy](#5-policy) · [Wallet](#6-wallet) · [Subscription](#7-subscription) · [BYOK](#8-byok) · [Admin](#9-admin) · [Website-builder / Preview](#10-website-builder--preview) · [Images](#27-images-генерация-изображений-adr-058) · [Health / Docs](#11-health--docs) · [Chats](#17-chats) · [Profile](#18-profile) · [Preferences](#19-preferences) · [Tokens](#20-tokens)
5. [blockReason — справочник (10 значений)](#12-blockreason--справочник)
6. [Tool-протокол: client-side vs server-side](#13-tool-протокол)
7. [Монетизация (кратко)](#14-монетизация-кратко)
8. [Превью сайта для iOS](#15-превью-сайта-для-ios)
9. [Лимиты и rate limits](#16-лимиты-и-rate-limits)
10. [Как тестировать через Swagger](#23-как-тестировать-через-swagger)

---

## 1. Базовые принципы

- **Один HTTP-запрос — один логический шаг.** Чат работает по протоколу tool-use: backend может вернуть запрос на исполнение инструмента (tool_call), iOS исполняет и присылает результат.
- **Бизнес-блокировки — это НЕ ошибки.** Если пользователю нельзя сгенерировать сообщение (нет кредитов, истёк trial и т.п.), сервис отвечает `200 OK` с `status="blocked"` и машиночитаемым `blockReason`. См. [раздел 12](#12-blockreason--справочник). HTTP 4xx/5xx — только технические ошибки.
- **Деньги/кредиты — целые числа.** 1 кредит = 1 сообщение. Дробей нет.
- **Изоляция данных.** Пользователь видит только свои ресурсы — это обеспечивается сверкой `userId` тела с `sub` JWT.

---

## 2. Аутентификация и заголовки

### Независимые контуры авторизации

| Контур | Кто использует | Механизм | Заголовок | Swagger security scheme |
|---|---|---|---|---|
| **Пользовательский** | iOS-приложение, эндпоинты `/v1/*` (кроме `/v1/auth/*`, admin, preview, adapty-webhook) | JWT Bearer (RS256) | `Authorization: Bearer <JWT>` | `bearerAuth` (http bearer, format JWT) |
| **Admin** | Операторские/саппорт-инструменты, `/v1/admin/*` | статический секрет | `X-Admin-Token: <ADMIN_API_SECRET>` | `adminToken` (apiKey, header `X-Admin-Token`) |
| **Preview** | Браузер (открывает превью сайта) | подпись внутри URL (HMAC+TTL) | нет — авторизация в самой ссылке | — (публичный по signed URL) |
| **Adapty webhook** | Сервис Adapty (M2M), `/v1/billing/adapty/webhook` | статический bearer-секрет (без HMAC-подписи payload) | `Authorization: Bearer <ADAPTY_WEBHOOK_SECRET>` | отдельная http-bearer схема ([ADR-029](adr/ADR-029-adapty-subscription-webhook.md)) |
| **CloudPayments webhook** | Агрегатор broadapps/YooKassa (RU-путь), `/v1/billing/cloudpayments/webhook` | **публичный (нет 401), [ADR-054](adr/ADR-054-cloudpayments-webhook-payment-verification.md)** — broadapps шлёт колбэк без auth/подписи | нет обяз. заголовка; trust-anchor = **верификация через broadapps API**; per-IP rate-limit | начисление только по подтверждённому `succeeded`-платежу; гейт активации `CLOUDPAYMENTS_API_TOKEN`; активен только на avelyra |

> Эндпоинты выпуска токена `/v1/auth/register|token|refresh|apple` и `GET /v1/auth/jwks` — **public** (без `Authorization`): это точка получения JWT. Защита — per-IP rate-limit. См. [§21](#21-auth-выпуск-токена).

Контуры **взаимно изолированы**: пользовательский JWT не даёт доступа к admin-эндпоинтам, admin-токен — к пользовательским ресурсам. Эскалация невозможна by design.

### Пользовательский JWT (RS256)

- Алгоритм подписи — **RS256** (асимметричный: приватный ключ — секрет подписи, публичный — для verify).
- **Issuer встроен в backend** ([ADR-018](adr/ADR-018-embedded-auth-issuer.md)): токен выпускается через `/v1/auth/*` (см. [§21](#21-auth-выпуск-токена)) и верифицируется тем же сервисом (self-consistent, `iss=https://broadnova.shop`, `aud=claude-ios`).
- Обязательные claims: `sub` = `userId` (UUID), `exp` (срок), `iat`, `device_id`, `iss`, `aud`. Заголовок `kid`.
- Просроченный/невалидный токен → `401`.
- `userId` в теле запроса **обязан** совпадать с `sub` токена, иначе `403`.
- Первичная аутентификация — **device-based** (анонимная): клиент получает токен по `deviceId` через `POST /v1/auth/register`. Пользователь создаётся при register (явно) либо лениво при первом `/v1/*` запросе (источник идентичности — встроенный issuer).

### Admin-токен

- Статический высокоэнтропийный секрет `ADMIN_API_SECRET`, передаётся в `X-Admin-Token`.
- Сравнение — constant-time. Отсутствие/несовпадение → `401`.
- Поддержана ротация (второй секрет на grace-период).

### Preview signed URL

- Подпись `HMAC-SHA256` под отдельным секретом `PREVIEW_URL_SECRET` поверх `projectId|ownerUserId|exp`, с TTL (дефолт 15 минут).
- Открывается прямой ссылкой в браузере/`WKWebView`, без cookies и без JWT.

### Общие заголовки запроса

| Заголовок | Обязательность | Назначение |
|---|---|---|
| `Authorization: Bearer <JWT>` | обязателен для всех `/v1/*` (кроме `/v1/preview/*`) | пользовательская аутентификация |
| `X-Admin-Token` | обязателен для `/v1/admin/*` | admin-аутентификация |
| `X-Device-Id` | опционален для `/v1/chat/*` | override `device_id` для per-device rate limit; при отсутствии — fallback на `device_id` из JWT-claim. Если и claim пуст — per-device лимит не применяется |
| `Content-Type: application/json` | обязателен для POST | формат тела |
| `X-Request-Id` | опционален | correlation id одного HTTP-запроса (логи/трейсы). Если не передан — сервис генерирует и вернёт его в ответе. Это **не** ключ идемпотентности биллинга |
| `X-Scrape-Token` | только для `/metrics`, если включена защита токеном | доступ к метрикам |

### Что секрет и что не логируется

Никогда не логируются и не попадают в audit/трейсы: заголовок `Authorization` и JWT целиком, `X-Admin-Token`, BYOK API-ключ (`apiKey`), StoreKit `transaction`, `PREVIEW_URL_SECRET`, любые поля содержащие `key`/`token`/`secret`. В логах — только correlation id (`X-Request-Id`), `sessionId` и нечувствительные поля.

---

## 3. Коды ответа (общие)

| Код | Когда |
|---|---|
| **200** | успех; **в том числе бизнес-blocked** для `/v1/chat/*` (тело со `status="blocked"`, `blockReason`) |
| **401** | нет/невалидный/просроченный JWT; нет/неверный `X-Admin-Token` |
| **403** | `userId` в теле ≠ `sub` JWT; для preview — невалидная подпись/истёкший URL/чужой владелец |
| **404** | ресурс/сессия/пользователь не найдены; для preview — проект или файл не найдены |
| **409** | конфликт идемпотентности (тот же ключ — другой payload); недостаточно кредитов на момент списания |
| **413** | превышен общий размер тела запроса (transport-уровень, до парсинга) |
| **422** | невалидная схема/значение поля; превышен лимит отдельного поля; невалидная StoreKit-транзакция |
| **429** | превышен rate limit |
| **502 / 5xx** | внутренняя ошибка или ошибка upstream (Anthropic / App Store / KMS) |

Стандартный формат технической ошибки (4xx/5xx):
```json
{ "error": { "code": "validation_error", "message": "human readable", "requestId": "..." } }
```
`code` ∈ `unauthorized | forbidden | not_found | conflict | payload_too_large | validation_error | rate_limited | internal_error | upstream_error`.

> Бизнес-блокировки **не** используют этот формат — они приходят как `200` со `status="blocked"`.

---

## 4. Chat

Основной поток: пользователь шлёт сообщение, backend оркестрирует диалог с Claude, при необходимости запрашивает исполнение инструментов. Сервис — прежде всего **чат-агрегатор Claude**; работает **без `projectId`** (website-builder — опциональная фича, [ADR-022](adr/ADR-022-optional-project-and-tool-gating.md)).

> **План dialog-modes/temporary/quizzes/images ([ADR-055](adr/ADR-055-dialog-mode.md)..[ADR-059](adr/ADR-059-openai-default-provider.md)) — поставлен полностью (спринты 2–5).** Все поля контракта доступны в текущей сборке: `dialogMode` + режимы `smart`/`deep_thinking`/`search` + `422 unsupported_dialog_mode` + `defaultDialogMode` ([ADR-055](adr/ADR-055-dialog-mode.md)); `temporary`/`history` (временный чат, [ADR-056](adr/ADR-056-temporary-chat.md)); `quiz` (**пул вопросов** `quiz.questions[]`, [ADR-057](adr/ADR-057-study-learn-quiz-contract.md) + редизайн [ADR-062](adr/ADR-062-study-learn-quiz-pool.md)) + режим `study_learn`; `images` в ответе + `GET /v1/images/{id}` (§27) + `blockReason=image_credits_empty` ([ADR-058](adr/ADR-058-image-generation.md)). Провайдер по умолчанию — OpenAI Responses ([ADR-059](adr/ADR-059-openai-default-provider.md)).

### POST /v1/chat/run
Старт или продолжение агентного шага (отправка пользовательского сообщения).

**Заголовки:** `Authorization: Bearer <JWT>` (обязателен), `X-Device-Id` (опц.; override per-device rate-limit, иначе fallback на `device_id` из JWT-claim), `Content-Type: application/json`, `X-Request-Id` (опц.).

**Request:**
| Поле | Тип | Прим. |
|---|---|---|
| `userId` | string (uuid) | = `sub` JWT |
| `projectId` | string, **опц.** | Ключ проекта website-builder. **Опционален** ([ADR-022](adr/ADR-022-optional-project-and-tool-gating.md)): без него — обычный чат-агрегатор (основной поток), server-side `site.*` tools Claude **не** предлагаются; с ним — доступен website-builder (`site.*`). Фиксируется при создании сессии; при resume берётся из сессии (поле запроса игнорируется). На биллинг/policy не влияет. |
| `sessionId` | string (uuid), опц. | нет → создаётся новая сессия, `mode` фиксируется на сессию |
| `message` | string, **опц.** | Текст сообщения. ≤ 32 KB. **Опционален при наличии вложений** ([ADR-039](adr/ADR-039-optional-message-with-attachments.md)): валидно, если `message` непуст после `strip` **ИЛИ** есть ≥1 `attachments`; иначе → `422` `"message or at least one attachment is required"`. Пустой текст → отправляется только вложение (image-only / file-only ход), пустой text-блок провайдеру не шлётся. |
| `mode` | `credits` \| `byok` | **billing_mode** — способ оплаты генерации (фиксируется на сессию) |
| `assistantMode` | `chat` \| `code`, опц. | **assistant_mode** — тип ассистента (ADR-012). При отсутствии — дефолт из `GET /v1/preferences` (`defaultAssistantMode`), затем `chat`. Фиксируется при создании сессии. **Ортогонален `mode`** |
| `model` | string, **опц.** | **Выбор модели** ([ADR-034](adr/ADR-034-user-model-selection.md)). Id из `GET /v1/models` (модели активного провайдера инстанса). **Session-fixed**: фиксируется при создании сессии; при resume берётся из сессии (поле запроса игнорируется). Без `model` → дефолтная модель инстанса (`ANTHROPIC_MODEL`/`OPENAI_MODEL`). Непустая после `strip`; вне allowlist → **`422` с машинным кодом `error.code = "unsupported_model"`** ([ADR-034 §3 ревизия 2026-07-09](adr/ADR-034-user-model-selection.md), симметрично `unsupported_dialog_mode`; тихого фолбэка нет). На биллинг не влияет (1 кредит, [ADR-006](adr/ADR-006-credit-billing-and-subscription-grant.md)); `usage.model` отражает использованную модель. |
| `workspaceProjectId` | string (uuid), **опц.** | **Привязка чата к рабочему пространству** ([ADR-013](adr/ADR-013-workspace-projects-vs-website-builder.md)/[ADR-036](adr/ADR-036-workspaces-implementation.md)). **Session-fixed**: фиксируется при создании сессии; при resume берётся из сессии (поле запроса игнорируется). При создании валидируется принадлежность workspace пользователю → чужой/несуществующий = **`404 workspace_not_found`**. При наличии: `workspace.instructions` подмешиваются в system-prompt после base-промта; файлы-знания workspace подаются как контекст (document/text → извлечённый текст, image → vision). **≠ `projectId`** (website-builder). На биллинг не влияет. |
| `attachments` | array, опц. | **inline base64-вложения** (фото/PDF/текст), ≤ 10. Только в первом (новом) user-turn; в `/chat/tool-result` не принимаются. Каждый элемент: `{ type: image\|document\|text, mediaType, filename?, data (base64) }`. См. ниже. ([ADR-020](adr/ADR-020-inline-base64-attachments-mvp.md)) |
| `context` | object, опц. | **Per-message доп-настройки хода** ([ADR-037](adr/ADR-037-chatrunrequest-context-allowlist-injection.md)). НЕ session-fixed — присылается на каждый `/chat/run`, может меняться по ходу чата (без БД). Allowlist: `codeLanguage` (str≤40), `responseStyle` (`concise\|balanced\|detailed`), `verbosity` (`low\|medium\|high`), `tone` (str≤40), `locale` (str≤35 BCP-47-подобный). Неизвестные ключи и невалидные значения — **игнорируются** (lenient). Инъектируется в текущее user-сообщение (НЕ в system). Служебный блок **не виден** в истории `GET /v1/chats/{id}` и превью `GET /v1/chats` — срезается при отдаче ([ADR-042](adr/ADR-042-hide-context-block-from-user-facing-history.md)); хранение/реплей модели не меняются. Без валидных ключей → поведение неизменно. Size ≤ 64 KB сериализованного JSON (иначе `422`). |
| `editMessageStepId` | string (uuid), **опц.** | **Редактирование отправленного сообщения** ([ADR-040](adr/ADR-040-edit-message-and-regenerate.md)). `messageStepId` хода, который надо отредактировать (берётся из `steps[].messageStepId` истории или `ChatResponse.messageStepId`). Backend усекает историю от этого хода (его user-шаг и всё после) и генерирует новый ход с переданными `message`/`attachments`/`context`. **Требует `sessionId`** (resume): без него → `422`. Чужая/несуществующая сессия → `404`; нет user-шага с этим `messageStepId` → `404 message_not_found`. Биллинг: новый ход = **новый дебит 1 кредита**, возврата за старый ход нет. См. callout ниже. |
| `dialogMode` | `smart` \| `deep_thinking` \| `study_learn` \| `search`, **опц.** | **Режим диалога** ([ADR-055](adr/ADR-055-dialog-mode.md)). **Session-fixed** (как `model`/`assistantMode`): фиксируется при создании сессии; при resume берётся из сессии (поле запроса игнорируется). Без `dialogMode` → `user_preferences.defaultDialogMode` → `smart`. **Ортогонален** `mode` (billing) и `assistantMode` (chat/code). `deep_thinking`/`study_learn`/`search` требуют OpenAI-инстанса ([ADR-059](adr/ADR-059-openai-default-provider.md)); на anthropic-инстансе → **`422 unsupported_dialog_mode`**. Влияние: `deep_thinking` — reasoning-модель инстанса; `search` — веб-поиск с цитатами; `study_learn` — квиз в поле `quiz` ответа ([ADR-057](adr/ADR-057-study-learn-quiz-contract.md)). Биллинг неизменен (1 кредит). |
| `temporary` | bool, **опц.** (дефолт `false`) | **Временный чат** ([ADR-056](adr/ADR-056-temporary-chat.md)): ход НЕ сохраняет **переписку** (ни `chat_sessions`/`chat_steps`/`tool_calls`), не виден в `GET /v1/chats`. Историю присылает клиент полем `history`. Разрешены только server-side инструменты (client-side отключены). **Взаимоисключения → `422`:** `temporary` с `sessionId`/`editMessageStepId`/`projectId`/`workspaceProjectId`. Ответ: `stepId=null`, `messageStepId` присутствует; **кредит списывается** (временный ≠ бесплатный) — дебит в `ledger_transactions` (привязан к `user_id`+`messageStepId`, ссылки на сессию не несёт) + запись `billing_debit` в `audit_logs` (`session_id=null`), а **первый ход потребляет пожизненный trial** (`users.trial_used`). То есть переписка не хранится, но остаются три не-контентных следа (ledger/audit/trial), ни один без текста диалога ([ADR-056 §4](adr/ADR-056-temporary-chat.md)). **`sessionId` в ответе — синтетический (непersisted):** его нельзя резюмировать — повторная отправка в `/v1/chat/run` без `temporary` игнорирует id и создаёт **новую** сессию, а в `/v1/chat/tool-result` → **`404`**. Контекст держится клиентом через `history`, не через `sessionId`. «Не обучается AI» — гарантия провайдера API (+`store=false`), не флаг запроса. |
| `history` | array `[{role: "user"\|"assistant", content: string}]`, **опц.** | **Транскрипт истории для временного чата** ([ADR-056](adr/ADR-056-temporary-chat.md)). Провайдер-агностичный текст (только user/assistant реплики; tool-раунды не передаются). Применяется **только** с `temporary=true`; `history` без `temporary` → **`422`**. **Валидация:** если `history` непуста, её **первая** реплика обязана иметь `role == "user"`, иначе → **`422`** (Anthropic жёстко требует, чтобы первое сообщение было от user; без проверки провайдер вернул бы `400` → непрозрачный `502` вместо честного `422`). **Строгое чередование ролей НЕ требуется** — подряд идущие реплики одной роли допустимы (оба провайдера это принимают; Anthropic объединяет соседние сообщения одной роли). Размер ≤ `size_limit_context`. |

> **`assistantMode` ≠ `mode`.** `assistantMode` (`chat`/`code`) — *какой* ассистент отвечает (продуктовый профиль ответа), задаётся клиентом или берётся из preferences. `mode` (`credits`/`byok`) — *чем платим* за генерацию. Это два независимых измерения: возможна любая из 4 комбинаций (напр. `code`+`byok`). Подробнее — [ADR-012](adr/ADR-012-assistant-mode-vs-billing-mode.md).

> **Вложения (`attachments[]`, [ADR-020](adr/ADR-020-inline-base64-attachments-mvp.md)).** Мультимодальный ввод передаётся inline в base64 (без отдельного upload-эндпоинта). Классы и allowlist `mediaType`:
> - `type: image` — `image/jpeg`, `image/png`, `image/gif`, `image/webp` → Claude vision;
> - `type: document` — `application/pdf` → нативный PDF-разбор Claude;
> - `type: text` — `text/plain`, `text/markdown`, `text/csv`, `application/json` → инлайн как текст с разметкой имени файла.
>
> **Отправка без текста ([ADR-039](adr/ADR-039-optional-message-with-attachments.md)).** `message` можно оставить пустым, если есть ≥1 вложение (image-only / file-only ход) — UI «отправить фото/файл без подписи». Backend шлёт провайдеру только attachment-блоки (пустой text-блок не отправляется). На OpenAI-инстансе работает и image-only, и PDF-only ([ADR-041](adr/ADR-041-openai-native-pdf-attachment.md), см. ниже).
>
> MIME вне allowlist → `422`; рассогласование заявленного `mediaType` и реального содержимого (magic bytes) → `422`; невалидный base64 → `422`. URL-вложения не поддерживаются (только inline). Лимиты (дефолты, конфигурируемы): ≤ 10 вложений; одно ≤ 5 MB (image) / 8 MB (document); суммарно ≤ 10 MB; PDF ≤ 100 страниц; тело `/v1/chat/run` ≤ 12 MB (повышенный лимит **только** этого роута). **Биллинг неизменен: сообщение с вложениями = 1 кредит** ([ADR-006](adr/ADR-006-credit-billing-and-subscription-grant.md)). Содержимое вложений не логируется.
>
> **PDF на обоих провайдерах ([ADR-041](adr/ADR-041-openai-native-pdf-attachment.md), снято прежнее ограничение [ADR-033](adr/ADR-033-llm-provider-abstraction.md)/[TD-023](100-known-tech-debt.md)).** Сервис разворачивается мульти-инстансно на разных LLM-провайдерах одним кодом (выбор — env `LLM_PROVIDER`, дефолт `anthropic`). `type: document` (`application/pdf`) теперь принимается на **обоих** провайдерах: на **anthropic** — нативный `document`-блок (как прежде); на **OpenAI** (`LLM_PROVIDER=openai`, Chat Completions) — нативная content-часть `file` (data-URI `application/pdf`) либо извлечённый `pypdf`-текст как text-блок (фолбэк). `image`/`text` работают на обоих (картинки — через `image_url` data-URI). Контракт `attachments[]` един для обоих провайдеров; PDF можно отправлять на любой инстанс.
>
> Пример `attachments[]`:
> ```json
> [
>   { "type": "image", "mediaType": "image/png", "filename": "task.png", "data": "<base64>" },
>   { "type": "document", "mediaType": "application/pdf", "filename": "report.pdf", "data": "<base64>" }
> ]
> ```
>
> **Редактирование отправленного сообщения (`editMessageStepId`, [ADR-040](adr/ADR-040-edit-message-and-regenerate.md)).** Чтобы изменить ранее отправленное сообщение, клиент шлёт **новый** `POST /v1/chat/run` с тем же `sessionId`, новым `message` (и/или `attachments`/`context`) и `editMessageStepId` = `messageStepId` редактируемого хода (из истории `GET /v1/chats/{id}` → `steps[].messageStepId`, либо из `ChatResponse.messageStepId`, полученного при отправке). Backend атомарно усекает историю от этого хода (его user-шаг **и всё после**) и генерирует заново. Используется именно `messageStepId` (ход целиком), **не** `stepId`. Требует `sessionId` (без него → `422`); чужая/несуществующая/истёкшая сессия → `404`; нет user-шага с этим `messageStepId` → `404 message_not_found`. **Биллинг: регенерация = новый ход = новый дебит 1 кредита; возврата за удалённый старый ход нет** (no-refund-on-edit). Открытый tool-loop редактируемого/последующего хода корректно сбрасывается. Без `editMessageStepId` поведение `/chat/run` неизменно.

**Response (200):**
| Поле | Тип | Прим. |
|---|---|---|
| `status` | `assistant_message` \| `tool_call` \| `blocked` | исход шага |
| `sessionId` | string (uuid) | сессия (новая или переданная) |
| `messageStepId` | string (uuid) \| null | ключ **хода** (один на сообщение, переиспользуется во всех tool-раундах); `null` при `blocked`. Совпадает с `steps[].messageStepId` в истории. ([ADR-023](adr/ADR-023-sync-ids-in-chat-response.md)) |
| `stepId` | string (uuid) \| null | id **конкретного** assistant/tool-шага этого ответа; `null` при `blocked`. Совпадает с `steps[].id` в истории `GET /v1/chats/{id}`. ([ADR-023](adr/ADR-023-sync-ids-in-chat-response.md)) |
| `assistantMessage` | string, опц. | присутствует при `assistant_message`; **также при `tool_call`**, если Claude выдал текст вместе с `tool_use` — текст того же assistant-шага (`stepId`); `null`/опущено, если текста не было ([Q-024-1](99-open-questions.md) / [ADR-024](adr/ADR-024-history-payload-domain-normalization.md)). **При наличии `quiz` в ответе — детерминированно `null`** (backend отбрасывает свободный текст квиз-хода: без дубля вопросов/спойлера ответа, [ADR-062 §3](adr/ADR-062-study-learn-quiz-pool.md)) |
| `toolCalls` | array `[{ id, name, args }]`, опц. | **присутствует при `tool_call` — ВСЕ client-side tool-вызовы хода** (parallel tool use). Клиент обязан исполнить и вернуть результаты на все элементы ([ADR-025](adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)). Server-side `site.*` сюда не входят. |
| `toolCall` | object `{ id, name, args }`, опц. (**deprecated**) | присутствует при `tool_call`, **= `toolCalls[0]`**; читайте `toolCalls[]`. `id` — публичный UUID для `/chat/tool-result` (≠ `stepId`) |
| `serverTools` | array `[{ toolCallId, toolName, status, summary? }]` | **server-side инструменты (`site.*`/`time.now`), выполненные backend за ЭТОТ вызов** ([ADR-028](adr/ADR-028-projectid-in-chat-list-and-server-tools-in-chat-response.md)). Дополняет `toolCalls[]` (там — только client-side). `toolCallId` ([ADR-030](adr/ADR-030-toolcallid-in-server-tools.md)) — доменный uuid4 (= `tool_calls.id`), обязательный; **совпадает** с `toolCallId` соответствующего tool-шага истории `GET /v1/chats/{id}` (`steps[].payload.toolCallId`) → корреляция записи с историей; тот же домен id, что у `toolCalls[].id` (client-side). `status` = `completed`\|`errored`; `summary` — компактный итог (≤120, **без raw/путей/URL/токенов**; полный результат — в истории). Присутствует при `assistant_message`/`tool_call`/`blocked` (хотя бы `[]`); пустой `[]` при policy-`blocked`; может быть НЕ пустым при `max_tokens`. Биллинг неизменен. Семантика «за один вызов», не за сессию |
| `blockReason` | enum, опц. | присутствует при `blocked` (см. [раздел 12](#12-blockreason--справочник)); `max_tokens` = обрезка ответа ([ADR-025](adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)) |
| `usage` | object `{ inputTokens, outputTokens, model }` | при `assistant_message`/`tool_call`; **также при `blocked`+`blockReason=max_tokens`**; нет при policy-`blocked` |
| `quiz` | object `{ questions: [ { question, options[], correctIndex, explanation } ] }` \| null, опц. | **Квиз-ПУЛ режима Study & Learn** ([ADR-057](adr/ADR-057-study-learn-quiz-contract.md) + редизайн [ADR-062](adr/ADR-062-study-learn-quiz-pool.md); breaking, пред-релиз). Присутствует только при `dialogMode=study_learn`, когда модель сгенерировала квиз; иначе `null`. `questions` — **3..10** вопросов; каждый: `options` 2..10, `correctIndex < len(options)` (проверка ответа — **на клиенте, локально**; эндпоинта отправки ответов нет). iOS итерирует `quiz.questions[]`. **При наличии `quiz` поле `assistantMessage` детерминированно `null`** (без дубля вопросов и спойлера ответа, [ADR-062 §3](adr/ADR-062-study-learn-quiz-pool.md)). **Всегда валиден** (или `null`): невалидный пул от модели не роняет ход — backend просит модель исправить **весь пул** в том же ходе (graceful degrade all-or-nothing, [ADR-062 §7](adr/ADR-062-study-learn-quiz-pool.md)/[ADR-057 §3](adr/ADR-057-study-learn-quiz-contract.md)). |
| `images` | array `[{ imageId, contentType, size }]`, опц. | **Сгенерированные изображения хода** ([ADR-058](adr/ADR-058-image-generation.md)). Элемент — **строго `imageId`/`contentType`/`size`** (без `prompt`, приватность — [ADR-058 §7](adr/ADR-058-image-generation.md)); байты не в ответе — фетчить `GET /v1/images/{imageId}`. Каждое изображение = **отдельный дебет `IMAGE_CREDITS_COST`**, списывается **независимо от `mode`** — **и при `credits`, и при `byok`** (изображение генерируется на серверном ключе, [ADR-058 §4](adr/ADR-058-image-generation.md)); BYOK покрывает своим ключом только текст. Итог: `credits` → `1 + N×COST`, `byok` → `0 (текст) + N×COST`. При нехватке кредитов (в любом режиме, включая trial) → `status=blocked`, `blockReason=image_credits_empty` (байты не сохранены). Ошибки генерации деградируют в tool-ошибку (ход не падает). Генератор — OpenAI `gpt-image-1`, **активен если задан `OPENAI_API_KEY`** (независимо от `LLM_PROVIDER` — Anthropic-инстанс с ключом тоже генерирует, [ADR-058 §3](adr/ADR-058-image-generation.md)). |

> **Синхронизация с историей чата ([ADR-023](adr/ADR-023-sync-ids-in-chat-response.md)).** `messageStepId`/`stepId` дают клиенту ключ для склейки ответа генерации с шагами `GET /v1/chats/{id}` → `steps[]`: `stepId` = точный шаг (`steps[].id`), `messageStepId` = ход для группировки tool-loop-раундов (`steps[].messageStepId`). При `status=blocked` шаг/ход не создаются (блок до генерации) → оба `null`. На `/v1/chat/tool-result` `messageStepId` стабилен в пределах хода (равен исходному `/chat/run`), `stepId` — id нового шага этого ответа.

Значения `status`:
- **`assistant_message`** — финальный текстовый ответ Claude. На этом шаге списывается 1 кредит (для `mode=credits`). **Квиз-ход ([ADR-062 §3](adr/ADR-062-study-learn-quiz-pool.md)):** если в ответе есть `quiz` (не `null`) — `assistantMessage` детерминированно `null` (свободный текст модели отброшен; контент несёт `quiz.questions[]`, рамку рисует клиент).
- **`tool_call`** — Claude запросил исполнение client-side инструмента(ов); iOS обязан исполнить **все** `toolCalls[]` и вернуть результаты через `/v1/chat/tool-result` (батч). Кредит на промежуточных tool-раундах не списывается. Server-side инструменты (`site.*`/`time.now`) исполняет backend сам и наружу как `tool_call` не отдаёт — но факт их выполнения за этот вызов виден в `serverTools[]` ([ADR-028](adr/ADR-028-projectid-in-chat-list-and-server-tools-in-chat-response.md)). **Несколько tool-вызовов в одном ходе (parallel tool use):** все возвращаются в `toolCalls[]` — backend продолжит диалог только после получения результатов на **все** из них ([ADR-025](adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)). **Если Claude в том же ходе выдал текст вместе с `tool_use`, он возвращается в `assistantMessage`** (тот же шаг, `stepId`) ([ADR-024](adr/ADR-024-history-payload-domain-normalization.md)); **исключение — при наличии `quiz` в ответе `assistantMessage` детерминированно `null`** ([ADR-062 §3](adr/ADR-062-study-learn-quiz-pool.md)).
- **`blocked`** — генерация запрещена бизнес-правилом; смотри `blockReason`. **Особый случай `blockReason=max_tokens`** ([ADR-025](adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)): ответ Claude обрезан лимитом output-токенов; инструменты не отдаются (обрезаны), **кредит не списывается**, `usage`/`messageStepId`/`stepId` присутствуют. UX: повторить или сократить запрос.

**Коды:** `200` (вкл. blocked); `401` (нет JWT); `403` (`userId ≠ sub`); `404` (сессия не найдена; `workspace_not_found`; `message_not_found` — `editMessageStepId` не резолвится в user-ход сессии, [ADR-040](adr/ADR-040-edit-message-and-regenerate.md)); `413` (тело > 12 MB — повышенный лимит этого роута под inline base64-вложения, [ADR-020](adr/ADR-020-inline-base64-attachments-mvp.md)); `422` (схема/`message` > 32 KB/`context` > 64 KB/вложение вне allowlist/невалидный base64/MIME-mismatch/PDF page-guard/`editMessageStepId` без `sessionId` — [ADR-040](adr/ADR-040-edit-message-and-regenerate.md)/**`unsupported_model`** — модель вне allowlist инстанса [ADR-034](adr/ADR-034-user-model-selection.md)/**`unsupported_dialog_mode`** — режим требует OpenAI-инстанса [ADR-055](adr/ADR-055-dialog-mode.md)/**`temporary` с `sessionId`/`editMessageStepId`/`projectId`/`workspaceProjectId` или `history` без `temporary`** — [ADR-056](adr/ADR-056-temporary-chat.md)); `429` (rate limit); `502/5xx` (ошибка провайдера/внутренняя).

---

### POST /v1/chat/tool-result
Приём результата client-side инструмента от iOS и продолжение того же шага.

**Заголовки:** как у `/v1/chat/run`.

**Request (батч — рекомендуется, [ADR-025](adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)):**
| Поле | Тип | Прим. |
|---|---|---|
| `userId` | string (uuid) | = `sub` |
| `sessionId` | string (uuid) | сессия шага |
| `results` | array `[{ toolCallId, result? , error? }]` | результаты на один/несколько tool-вызовов **одного хода**; в каждом элементе ровно одно из `result`/`error`; каждый `result` ≤ 256 KB |

**Request (одиночная форма — deprecated, обратная совместимость):** верхнеуровневые `toolCallId` + `result|error` (= `results` из одного элемента). Backend принимает обе формы.

**Барьер хода ([ADR-025](adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)):** backend продолжает диалог с Claude **только** когда собраны результаты на **все** `toolCalls[]` хода. Рекомендуется прислать все одним батчем. Можно частями (несколько запросов) — пока не все собраны, ответ снова `status=tool_call` с оставшимися `toolCalls[]` (без списания и без вызова Claude).

**Response (200):** та же схема, что у `/v1/chat/run` (следующий шаг — снова `assistant_message` / `tool_call` / `blocked`).

**Коды:** `200`; `401`; `403` (чужой `toolCallId`/`userId ≠ sub`); `404` (`toolCallId` не принадлежит сессии); `413`; `422` (`result` не соответствует схеме инструмента или > 256 KB); `429`; `502/5xx`.

---

## 5. Policy

### GET /v1/policy/effective
Эффективные права пользователя для отрисовки UI (что доступно, почему заблокировано).

**Заголовки:** `Authorization: Bearer <JWT>`. `userId` берётся из `sub` — query-параметров нет.

**Response (200):**
| Поле | Тип | Семантика |
|---|---|---|
| `isSubscribed` | bool | активная подписка |
| `trialRemaining` | int | 1, если нет подписки и trial не использован; иначе 0 |
| `creditsBalance` | int | текущий баланс кредитов |
| `byokEnabled` | bool | BYOK включён и ключ валиден |
| `canGenerateCreditsMode` | bool | можно ли генерировать в режиме credits |
| `canGenerateByokMode` | bool | можно ли генерировать в режиме byok |
| `reasons` | array | причины блокировки для недоступных режимов (подмножество blockReason **без** `rate_limited`) |

> `rate_limited` в `reasons[]` не входит — это транспортный концерн (HTTP `429`), а не бизнес-policy.

**Коды:** `200`; `401`; `429`; `5xx`.

---

## 6. Wallet

### GET /v1/wallet
Баланс и последние транзакции пользователя.

**Заголовки:** `Authorization: Bearer <JWT>`.

**Response (200):**
| Поле | Тип | Прим. |
|---|---|---|
| `balance` | int | текущий баланс |
| `lastTransactions` | array `{ id, type, amount, createdAt, meta }` | последние N (дефолт 20), `type` ∈ `credit`\|`debit`; `meta` без секретов |

**Коды:** `200`; `401`; `429`; `5xx`.

---

### POST /v1/wallet/consume
Списание кредитов (внутренний биллинговый контракт; штатно вызывается оркестратором, прямой клиентский вызов возможен, но не нужен в обычном flow).

**Заголовки:** `Authorization: Bearer <JWT>`.

**Request:**
| Поле | Тип | Прим. |
|---|---|---|
| `userId` | string (uuid) | = `sub` |
| `sessionId` | string (uuid) | сессия |
| `requestId` | string | **ключ идемпотентности** списания (для chat-debit это `messageStepId` шага, не `X-Request-Id`) |
| `amount` | int > 0 | целые кредиты; для chat-debit всегда 1 |
| `meta` | object | usage/model для аудита; на `amount` не влияет |

**Response (200):** `{ "newBalance": int, "ledgerTxId": "uuid" }`. Повторный тот же `requestId` с тем же payload → возвращает существующий `ledgerTxId` без повторного списания (идемпотентно).

**Коды:** `200`; `401`; `403`; `404` (`session_not_found`); `409` (тот же ключ — другой payload, либо `insufficient_credits`); `422`; `429`; `5xx`.

---

## 7. Subscription

### POST /v1/subscription/sync
Синхронизация статуса подписки по StoreKit-транзакции. Сервер верифицирует транзакцию (подпись/App Store Server API), не доверяя клиенту. При активации/продлении начисляется фиксированный пакет кредитов.

**Заголовки:** `Authorization: Bearer <JWT>`.

**Request:**
| Поле | Тип | Прим. |
|---|---|---|
| `userId` | string (uuid) | = `sub` |
| `transaction` | object | подписанный StoreKit payload (JWS / App Store receipt). **Не логируется** |

**Response (200):**
| Поле | Тип | Прим. |
|---|---|---|
| `isSubscribed` | bool | активна ли подписка |
| `expiresAt` | string (ISO8601) \| null | срок действия |
| `plan` | string \| null | план |

Идемпотентно по `transactionId` периода: повторный sync той же транзакции не начисляет кредиты повторно. Refund/revocation → `isSubscribed=false`.

**Коды:** `200`; `401`; `403`; `422` (невалидная/поддельная транзакция — подписка не меняется); `429`; `502/5xx` (ошибка App Store API).

> **Сосуществование с Adapty ([ADR-029](adr/ADR-029-adapty-subscription-webhook.md)):** `/v1/subscription/sync` **остаётся рабочим**, но источник истины по подпискам теперь — Adapty-вебхук (§7a). Клиент использует **ОДИН** путь подписок: на Adapty-сборке iOS **не** вызывает `sync` (иначе двойное начисление — разные idempotency-ключи).

---

## 7a. Billing — Adapty webhook ([ADR-029](adr/ADR-029-adapty-subscription-webhook.md))

### POST /v1/billing/adapty/webhook
Серверный вебхук Adapty (M2M, **вызывает Adapty, не iOS**) — **основной путь биллинга по подпискам**: по событию обновляет подписку и идемпотентно начисляет кредиты по тиру продукта.

**Авторизация:** `Authorization: Bearer <ADAPTY_WEBHOOK_SECRET>` (статический секрет, constant-time). **Adapty НЕ подписывает payload** (нет HMAC). Неверный/нет токена → `401`; секрет не сконфигурирован → `500`.

**Тело:** читается **сырым**, **без Pydantic-валидации** (Adapty при сохранении вебхука шлёт проверочный пинг с пустым/неполным телом и не сохранит вебхук без `2xx`). Распознаваемые поля (дефенсивно, по версиям Adapty): `event_id`‖`id`; `event_type` (→lower); `customer_user_id`‖`profile.customer_user_id`‖`user_id` (= наш `userId` UUID); `vendor_product_id` (из `event_properties.*`/корня); `expires_at` (опц.).

**После успешной авторизации любое тело → `2xx`** (Adapty ретраит не-2xx бесконечно). Тело ответа `{result, reason?, event_type?}`:

| HTTP | `result` | Когда |
|---|---|---|
| 401 | — | нет/неверный bearer |
| 500 | — | секрет не задан **или** реальный внутренний сбой (БД) → Adapty ретраит |
| 200 | `ignored` (`reason`: `empty_body`/`invalid_json`/`not_an_object`/`missing_event_id`/`missing_customer_user_id`/`user_not_found`) | кривой/неполный payload или неизвестный пользователь |
| 200 | `ignored` (+ `event_type` эхо) | неизвестный `event_type` |
| 200 | `duplicate` | повтор `event_id` |
| 200 | `applied` | событие применено |

**События (реальный формат Adapty, ADR-047):** GRANTING (`trial_started`/`subscription_started`/`subscription_renewed`/`access_level_updated`@`is_active=true,premium`) → `subscriptions.status=active` (+`plan`,`expiresAt` из `subscription_expires_at`) + грант кредитов по тиру; EXPIRING (`subscription_expired`/`subscription_cancelled`/`access_level_updated`@`is_active=false`) → `status=expired`, кредиты не трогаются; NOOP (`subscription_renewal_cancelled`/`trial_renewal_cancelled`) → доступ НЕ отзывается, кредиты не трогаются.

**Идемпотентность (ADR-047):** дедуп события — UNIQUE `adapty_webhook_events.event_id` (=`profile_event_id`); грант — **один на период** через ledger `adapty-txn:{transaction_id}` (не по `event_id`: одна покупка = несколько событий с одним `transaction_id`). Одна транзакция; сбой → откат → `5xx` → ретрай Adapty → чистая переобработка. Детали — [modules/billing-adapty/02-api-contracts.md](modules/billing-adapty/02-api-contracts.md).

---

## 7b. Billing — CloudPayments/broadapps (RU-путь: [ADR-051](adr/ADR-051-cloudpayments-checkout-payment-link.md) checkout + [ADR-050](adr/ADR-050-cloudpayments-webhook.md) webhook)

### POST /v1/billing/cloudpayments/checkout
**Наш** эндпоинт создания платёжной ссылки RU-оплаты ([ADR-051](adr/ADR-051-cloudpayments-checkout-payment-link.md)). **Вызывает iOS-клиент** (JWT). Делает исходящий вызов broadapps `POST /payments/link` и возвращает ссылку YooKassa. Активен **только на avelyra**.

**Авторизация:** пользовательский JWT (`Authorization: Bearer <JWT>`, `bearerAuth`). **`userId` = JWT `sub`, НЕ из тела** — фикс «потерянных платежей» (колбэк [ADR-050](adr/ADR-050-cloudpayments-webhook.md) находит пользователя по этому `userId`).

**Тело** (StrictModel): `productId` (валидируется allowlist `classify_product`; unknown/некредитуемый → `422`), `customerEmail` (`EmailStr`). Пример: `{"productId":"week_6.99_nottrial","customerEmail":"user@example.com"}`.

**Ответ `200`** (проброс broadapps): `{"paymentId","paymentUrl","status","expiresAt":null|str}`. `paymentUrl` — ссылка YooKassa (`https://yoomoney.ru/checkout/...`).

| HTTP | Код | Когда |
|---|---|---|
| 200 | — | broadapps вернул ссылку |
| 401 | `unauthorized` | нет/невалидный JWT |
| 422 | `validation_error` | неизвестный/некредитуемый `productId`, невалидный email, лишнее поле |
| 429 | `rate_limited` | лимит |
| 502 | `upstream_error` | broadapps недоступен/таймаут/не-2xx/malformed (без утечки токена/деталей) |
| 503 | `cloudpayments_checkout_not_configured` | `CLOUDPAYMENTS_APP_ID`/`CLOUDPAYMENTS_API_TOKEN` не заданы (⇒ только avelyra) |

**Исходящий вызов:** `POST {CLOUDPAYMENTS_API_BASE}/payments/link` (default `https://pay.broadapps.dev/api/v1`), multipart {`app_id`(config), `product_id`, `user_id`(=sub), `customer_email`}, `Authorization: Bearer <CLOUDPAYMENTS_API_TOKEN>` (**отдельный** от входящего `WEBHOOK_TOKEN`), таймаут 15с. `customer_email` (PII) и токен — не логируются/не в ответе. Контракт — сверить живьём ([Q-051-1](99-open-questions.md)). Детали — [modules/billing-cloudpayments/02-api-contracts.md](modules/billing-cloudpayments/02-api-contracts.md).

### POST /v1/billing/cloudpayments/webhook
Серверный вебхук агрегатора **broadapps** (`pay.broadapps.dev`, фронтит YooKassa) в формате **CloudPayments** (**вызывает broadapps, не iOS**) — **отдельный RU-путь**. **[ADR-054](adr/ADR-054-cloudpayments-webhook-payment-verification.md): эндпоинт ПУБЛИЧНЫЙ (нет `401`); колбэк = ТРИГГЕР, начисление — только после ВЕРИФИКАЦИИ платежей через broadapps API.** Активен **только на avelyra** (где задан `CLOUDPAYMENTS_API_TOKEN`).

**Авторизация ([ADR-054](adr/ADR-054-cloudpayments-webhook-payment-verification.md)):** **`401` НЕ выдаётся** — broadapps шлёт колбэк без авторизации (`authScheme=none`) и без HMAC-подписи → начислять по телу нельзя. `require_cloudpayments_webhook` — наблюдательная non-blocking (лог `cloudpayments_webhook_auth_observed`). **Trust-anchor начисления — верификация**, не токен. Публичный эндпоинт → **per-IP rate-limit** (`CLOUDPAYMENTS_WEBHOOK_RATE_LIMIT_PER_IP`, дефолт 120/мин, `429`). Гейт активации — `CLOUDPAYMENTS_API_TOKEN` (пуст → `500`). `CLOUDPAYMENTS_WEBHOOK_TOKEN` — легаси/опционален.

**Тело:** читается **сырым**. Из колбэка обязателен `AccountId`‖`Data.user_id` (**deviceId**, верхний регистр → lower → UUID) + гейт `Status=="Completed"`&`OperationType=="Payment"` (ci); `TransactionId`/`product_id` — опц. контекст лога. **Карт-данные не читаются/не логируются/не хранятся.**

**Верификация (единственный триггер начисления):** `deviceId`→`userId` (двухступенчатый, [ADR-053](adr/ADR-053-cloudpayments-webhook-user-resolution-via-auth-devices.md); `user_not_found` → `ignored`, **без** GET) → исходящий `GET {CLOUDPAYMENTS_API_BASE}/users/{deviceId}/payments` (`Bearer CLOUDPAYMENTS_API_TOKEN`, 15с) → **реконсиляция**: начислить каждый платёж со `status ∈ CLOUDPAYMENTS_PAID_STATUSES` (дефолт `succeeded`) в окне свежести (`CLOUDPAYMENTS_PAYMENT_FRESHNESS_HOURS`, дефолт 72ч), класс по `product.payment_type` (`one_time`→tokens \| `subscription`→subscription), сумма по `product.code` из серверных карт (anti-tamper), идемпотентно `cp-txn:{payment_id}`.

| HTTP | Тело | Когда |
|---|---|---|
| 429 | — | превышен per-IP лимит |
| 500 | — | `CLOUDPAYMENTS_API_TOKEN` не задан \| `api_error` broadapps (retriable) \| сбой БД → ретрай |
| 200 | `{"code":0}` | всё принятое: `applied` (`creditedCount≥1`) / `duplicate` / `ignored` (`empty_body`/`invalid_json`/`not_an_object`/`not_a_completed_payment`/`invalid_account_id`/`user_not_found`/`no_creditable_payment`) |

**Идемпотентность:** дедуп + грант по broadapps **`payment_id`** (ledger `cp-txn:{payment_id}`, дедуп-колонка `cloudpayments_webhook_events.transaction_id`:=`payment_id`), НЕ callback `TransactionId`. Продление — новый `payment_id` → новый грант. Детали — [modules/billing-cloudpayments/02-api-contracts.md](modules/billing-cloudpayments/02-api-contracts.md). Живая калибровка — [Q-054-1](99-open-questions.md)/[Q-054-2](99-open-questions.md).

---

## 8. BYOK

«Bring Your Own Key» — пользователь приносит собственный API-ключ **любого поддерживаемого провайдера** (Anthropic `sk-ant-…` ИЛИ OpenAI `sk-…`/`sk-proj-…`), **независимо** от провайдера инстанса ([ADR-044](adr/ADR-044-multi-provider-byok.md)). Провайдер определяется по формату ключа; валидация и генерация byok идут через этот провайдер. Ключ шифруется at-rest (envelope encryption), наружу plaintext никогда не возвращается и не логируется.

Общий формат ответа всех трёх эндпоинтов: `{ "byokEnabled": bool, "keyStatus": <enum>, "activeModel": string | null }`.

**`keyStatus` — 6 значений** ([ADR-016](adr/ADR-016-extended-byok-statuses.md)):

| keyStatus | Значение |
|---|---|
| `missing` | ключ не задан |
| `validating` | ключ сохранён, валидация в процессе |
| `valid` | ключ рабочий (прошёл проверку Anthropic) |
| `invalid` | ключ отклонён (Anthropic вернул 401) |
| `offline` | валидацию не удалось выполнить из-за сетевой ошибки (не финальный вердикт) |
| `expired` | ключ был `valid`, но впоследствии отозван/истёк |

> Старые клиенты, знающие только `valid`/`invalid`/`missing`, обязаны трактовать любой неизвестный статус как «не `valid`» (BYOK недоступен), не падая.

**`activeModel`** — строка с активной моделью при `keyStatus=valid`; значение = BYOK-дефолт **провайдера, определённого по ключу** (`claude-sonnet-4-6` для Anthropic-ключа, `gpt-4o` для OpenAI-ключа); во всех остальных статусах — `null`.

### POST /v1/byok/set
Сохранить и провалидировать ключ.
**Заголовки:** `Authorization: Bearer <JWT>`.
**Request:** `{ "userId": "uuid", "apiKey": "string (Anthropic sk-ant-… или OpenAI sk-…/sk-proj-…)" }` (`apiKey` ≤ 4 KB, не логируется).
**Поведение** ([ADR-044](adr/ADR-044-multi-provider-byok.md)): детект провайдера по префиксу ключа (`sk-ant-`→anthropic раньше `sk-`/`sk-proj-`→openai; иначе → `keyStatus=invalid` без сетевого вызова) → шифрование (AES-256-GCM + KMS-обёртка DEK) → upsert (+ сохранение провайдера) → лёгкая валидация вызовом **провайдера ключа** → `keyStatus=valid|invalid|offline`. Невалидный/offline ключ сохраняется со своим статусом, `byokEnabled` не включается.
**Коды:** `200`; `401`; `403`; `422`; `429`; `502/5xx`.

### POST /v1/byok/toggle
Включить/выключить использование BYOK.
**Заголовки:** `Authorization: Bearer <JWT>`.
**Request:** `{ "userId": "uuid", "enabled": bool }`.
**Поведение:** включить можно только при `keyStatus=valid`; иначе возвращается текущий статус без включения.
**Коды:** `200`; `401`; `403`; `422`; `429`; `5xx`.

### POST /v1/byok/delete
Удалить сохранённый ключ.
**Заголовки:** `Authorization: Bearer <JWT>`.
**Request:** `{ "userId": "uuid" }`.
**Response (200):** `{ "byokEnabled": false, "keyStatus": "missing", "activeModel": null }`.
**Коды:** `200`; `401`; `403`; `429`; `5xx`.

---

## 9. Admin

Операторские/саппорт-действия. **Авторизация — только `X-Admin-Token`** (пользовательский JWT не подходит). Отдельный rate limit (дефолт 10 req/min per source IP), тело ≤ 8 KB, строгая валидация. Admin-эндпоинты **не** создают пользователей.

### POST /v1/admin/wallet/grant
Ручное начисление кредитов пользователю (саппорт/компенсация).

**Заголовки:** `X-Admin-Token: <ADMIN_API_SECRET>` (обязателен), `Content-Type: application/json`.

**Request:**
| Поле | Тип | Прим. |
|---|---|---|
| `userId` | string (uuid) | существующий пользователь |
| `amount` | int > 0 | целые кредиты; `≤ 0` → `422` |
| `idempotencyKey` | string (≤ 128) | ключ идемпотентности начисления |
| `reason` | string (≤ 512) | **обязателен**; пишется в audit и meta |

**Response (200):**
| Поле | Тип | Прим. |
|---|---|---|
| `newBalance` | int | баланс после начисления |
| `ledgerTxId` | string (uuid) | id credit-транзакции |
| `idempotentReplay` | bool | `true`, если ключ уже использовался с тем же payload (повторного начисления не было) |

Каждое начисление пишет audit-событие `admin_grant` (без секрета токена).

**Коды:** `200`; `401` (нет/неверный `X-Admin-Token`); `404` (`user_not_found` — admin не создаёт пользователей); `409` (тот же `idempotencyKey`, другой `amount`); `422` (нет `reason` / `amount ≤ 0` / схема); `429` (admin rate limit); `5xx`.

### POST /v1/admin/subscription/grant
Ручная активация/продление подписки пользователю (саппорт/компенсация/тестирование) — **без** StoreKit-транзакции ([ADR-048](adr/ADR-048-admin-subscription-grant.md)). Нужно, потому что при отсутствии активной подписки доступ блокируется (`trial_used`) даже с ненулевым балансом — одного начисления кредитов мало.

**Заголовки:** `X-Admin-Token: <ADMIN_API_SECRET>` (обязателен), `Content-Type: application/json`.

**Request:**
| Поле | Тип | Прим. |
|---|---|---|
| `userId` | string (uuid) | существующий пользователь |
| `expiresAt` | string (ISO8601, tz-aware) | **ровно одно** из `expiresAt`/`days`; строго в будущем |
| `days` | int > 0 | **ровно одно** из `expiresAt`/`days`; `expires_at = now()+days` |
| `plan` | string (≤ 128) | опц.; дефолт `manual_grant` |
| `idempotencyKey` | string (≤ 128) | **обязателен**; ключ идемпотентности начисления |
| `credits` | int ≥ 0 | опц.; опущено → `SUBSCRIPTION_CREDITS_PER_PERIOD`; `0` → активировать без начисления |

**Response (200):**
| Поле | Тип | Прим. |
|---|---|---|
| `status` | string | `"active"` |
| `expiresAt` | string (ISO8601) \| null | эффективный срок |
| `plan` | string \| null | записанный план |
| `creditsGranted` | int | эффективно начислено (0 если нет) |
| `newBalance` | int \| null | только при `creditsGranted > 0` |
| `ledgerTxId` | string (uuid) \| null | только при `creditsGranted > 0` |
| `idempotentReplay` | bool \| null | только при `creditsGranted > 0` |

Upsert `subscriptions` (`status='active'`, `plan`, `expires_at`) **без** StoreKit-верификации; опц. начисление через тот же идемпотентный `WalletService.grant`; всё в одной транзакции. Audit `admin_subscription_grant` (без секрета).

**Коды:** `200`; `401`; `404` (`user_not_found`); `409` (тот же `idempotencyKey`, другой `credits`); `422` (нет `userId` / оба|ни одного из `expiresAt`/`days` / `expiresAt` не tz-aware|в прошлом / `days ≤ 0` / `credits < 0` / схема); `429`; `5xx`.

### GET /v1/admin/wallet/{userId}
Read-only просмотр кошелька для саппорта.

**Заголовки:** `X-Admin-Token` (обязателен).

**Path:** `userId` — UUID пользователя.

**Response (200):**
```json
{ "userId": "uuid", "balance": 1100,
  "lastTransactions": [ { "id": "uuid", "type": "credit|debit", "amount": 100, "createdAt": "ISO8601", "meta": {} } ] }
```

**Коды:** `200`; `401`; `404` (`user_not_found`); `429`; `5xx`.

---

## 10. Website-builder / Preview

> **Опциональная фича ([ADR-022](adr/ADR-022-optional-project-and-tool-gating.md)).** Основной поток сервиса — чат-агрегатор без проекта. Website-builder активируется **только** когда сессия создана с `projectId`; без проекта server-side `site.*` Claude не предлагаются и эта часть не задействуется.

Две части: **(A) server-side инструменты `site.*`** (исполняет backend, наружу как HTTP не торчат — это инструменты внутри chat tool-loop) и **(B) публичный preview-эндпоинт** для открытия сгенерированного сайта.

### A. Server-side инструменты `site.*`

Исполняются **backend'ом** внутри tool-loop `/chat/run` — iOS их **не** исполняет и не видит как `tool_call`. `userId` и проект берутся из серверного контекста сессии (модель не может записать в чужой проект). **Предлагаются Claude только при наличии `chat_sessions.project_id`** ([ADR-022](adr/ADR-022-optional-project-and-tool-gating.md)); в «чистом чате» (без `projectId`) отсутствуют. Перечислены здесь для понимания возможностей сервиса; интеграция iOS с ними прямая не требуется.

| Инструмент | Тип | Назначение |
|---|---|---|
| `site.write_file` | mutate (audit) | записать/перезаписать файл сайта (`path`, `content`, `contentType`, `encoding`) |
| `site.preview` | utility | сгенерировать signed URL превью (`{ url, expiresAt }`); `url` — **абсолютный** `https://<SERVICE_DOMAIN>/v1/preview/...` ([ADR-031](adr/ADR-031-absolute-preview-url.md)) |
| `site.list` | read | список файлов проекта |
| `site.read` | read | прочитать файл |
| `site.delete` | mutate (audit) | удалить файл |

Ошибки исполнения инструмента (превышение лимита, неверный content-type, path-traversal, файл не найден) возвращаются Claude как `is_error` внутри loop, **не** как HTTP-ошибка пользователю.

### B. GET /v1/preview/{projectId}/{token}/{path}
Публичная отдача статики сгенерированного сайта по signed URL. **Без пользовательского JWT** — авторизация в подписи.

**Заголовки запроса:** не требуются (публичный).

**Path-параметры:**
| Параметр | Прим. |
|---|---|
| `projectId` | внутренний UUID проекта |
| `token` | `<exp>.<hmac>` — срок + HMAC-SHA256 подпись |
| `path` | относительный путь файла (может содержать `/`) |

**Response (200):** тело файла; `Content-Type` строго из сохранённого `content_type` (allowlist). Заголовки безопасности: `Content-Security-Policy: sandbox allow-scripts allow-forms; default-src 'self'; frame-ancestors 'self'`, `X-Content-Type-Options: nosniff`, `X-Frame-Options: SAMEORIGIN`, `Cache-Control: private, no-store`. **Без `Set-Cookie`**.

**Коды:** `200`; `403` (подпись невалидна / TTL истёк / чужой владелец); `404` (проект или файл не найдены — намеренно `404`, чтобы не раскрывать существование чужих ресурсов).

---

## 11. Health / Docs

Служебные эндпоинты — **без авторизации** (кроме опц. защиты `/metrics`).

| Метод | Путь | Auth | Назначение | Ответ |
|---|---|---|---|---|
| GET | `/health` | нет | liveness (процесс жив) | `200 {status:"ok"}` |
| GET | `/healthz` | нет | **алиас `/health`** для healthcheck Traefik/smoke ([ADR-017](adr/ADR-017-shared-server-traefik-deploy.md)) | `200 {status:"ok"}` |
| GET | `/ready` | нет | readiness (БД + Redis доступны) | `200 {db:"ok",redis:"ok"}` или `503` |
| GET | `/metrics` | scrape-токен/сеть | Prometheus-метрики | exposition; `403` без `X-Scrape-Token` (если включён) |
| GET | `/docs`, `/redoc`, `/openapi.json` | нет (управляется флагом) | OpenAPI-документация (на русском, JWT scheme, теги по модулям) | схема API; `404` если `DOCS_ENABLED=false` (рекомендация для prod) |

---

## 17. Chats

История и управление чатами пользователя поверх существующих сессий (`chat_sessions`/`chat_steps`/`tool_calls`). Все эндпоинты строго скоупятся `sub` JWT: чужой/несуществующий чат → `404` (существование чужих ресурсов не раскрывается). Сортировка списка: закреплённые сверху (`isPinned`), затем по свежести (`updatedAt`).

**Общие заголовки:** `Authorization: Bearer <JWT>` (обязателен).

### GET /v1/chats
Список чатов с пагинацией и поиском.

**Query-параметры:**
| Параметр | Тип | Прим. |
|---|---|---|
| `q` | string, опц. | поиск по заголовку и тексту первого сообщения |
| `cursor` | string, опц. | непрозрачный (opaque) курсор следующей страницы — берётся из `nextCursor` предыдущего ответа |
| `limit` | int, опц. | размер страницы, 1..100 (дефолт 30) |
| `workspaceProjectId` | string (uuid), опц. | **фильтр «чаты проекта»** ([ADR-036](adr/ADR-036-workspaces-implementation.md)): только чаты, привязанные к этому workspace. Чужой/несуществующий → пустой список (изоляция по `sub`). Без параметра — все чаты |

**Response (200):**
| Поле | Тип | Прим. |
|---|---|---|
| `items` | array | элементы текущей страницы |
| `items[].id` | string (uuid) | идентификатор чата |
| `items[].title` | string \| null | заголовок (автоген из первого сообщения; null до генерации) |
| `items[].preview` | string \| null | срез текста последнего сообщения. Для user-сообщения **без** ведущего conversation-settings блока ([ADR-042](adr/ADR-042-hide-context-block-from-user-facing-history.md)): служебный блок `[Conversation settings for this message: …]` ([ADR-037](adr/ADR-037-chatrunrequest-context-allowlist-injection.md)) срезается при отдаче |
| `items[].assistantMode` | `chat` \| `code` | тип ассистента сессии |
| `items[].isPinned` | bool | закреплён ли чат |
| `items[].projectId` | string \| null | свободная строка website-builder-проекта (`= chat_sessions.project_id`, [ADR-022](adr/ADR-022-optional-project-and-tool-gating.md)/[ADR-028](adr/ADR-028-projectid-in-chat-list-and-server-tools-in-chat-response.md)); тот же формат, что `projectId` в `/chat/run`. `null` = «чистый чат» (без проекта). ≠ `workspaceProjectId` |
| `items[].workspaceProjectId` | string (uuid) \| null | привязка к рабочему пространству ([ADR-036](adr/ADR-036-workspaces-implementation.md)) — реальное значение `chat_sessions.workspace_project_id` (`null` = чат без workspace); **не** website-builder `projectId` ([ADR-013](adr/ADR-013-workspace-projects-vs-website-builder.md)) |
| `items[].updatedAt` | string (ISO8601) | время последнего обновления |
| `nextCursor` | string \| null | курсор следующей страницы; `null`, если страниц больше нет |

**Коды:** `200`; `401`; `422` (`limit` вне диапазона/битый `cursor`); `429`; `5xx`.

### GET /v1/chats/{id}
История шагов чата (упорядочены по времени).

**Path:** `id` — UUID чата.

**Response (200):**
| Поле | Тип | Прим. |
|---|---|---|
| `id` | string (uuid) | идентификатор чата |
| `title` | string \| null | заголовок |
| `assistantMode` | `chat` \| `code` | тип ассистента (assistant_mode) |
| `mode` | `credits` \| `byok` | режим оплаты сессии (billing_mode) |
| `steps` | array | шаги, упорядоченные по `chat_steps.seq` (порядок вставки, ADR-021); `createdAt` — информационный timestamp |
| `steps[].id` | string (uuid) | идентификатор шага |
| `steps[].messageStepId` | string (uuid) | message-шаг (биллинг-ключ) |
| `steps[].role` | `user` \| `assistant` \| `tool` | роль шага |
| `steps[].payload` | object | content-блоки шага (без raw provider id). У шага `role="user"` ведущий conversation-settings блок ([ADR-037](adr/ADR-037-chatrunrequest-context-allowlist-injection.md)) **срезается при отдаче** ([ADR-042](adr/ADR-042-hide-context-block-from-user-facing-history.md)) — в `text` остаётся только текст пользователя; хранение/реплей модели не меняются |
| `steps[].usage` | object \| null | потребление токенов (для assistant-шагов) |
| `steps[].createdAt` | string (ISO8601) | время создания шага |

**Коды:** `200`; `401`; `404` (чужой/несуществующий чат); `429`; `5xx`.

### GET /v1/chats/{id}/steps
Steps-view — агрегированные шаги одного message-шага (tool-calls/reasoning) для UI («N steps»). Только доменные имена инструментов, без raw provider id.

**Path:** `id` — UUID чата.
**Query:** `messageStepId` (uuid, опц.) — конкретный message-шаг; по умолчанию последний.

**Response (200):**
| Поле | Тип | Прим. |
|---|---|---|
| `messageStepId` | string (uuid) | message-шаг, для которого построен steps-view |
| `stepCount` | int | число шагов |
| `steps[].kind` | `reasoning` \| `tool_call` \| `tool_result` \| `assistant_message` | тип шага для UI |
| `steps[].toolName` | string \| null | доменное имя инструмента (с точкой) или `null` |
| `steps[].summary` | string | краткое человекочитаемое описание |
| `steps[].createdAt` | string (ISO8601) | время шага |

**Коды:** `200`; `401`; `404`; `429`; `5xx`.

### PATCH /v1/chats/{id}
Переименование, закрепление и/или **перенос чата в воркспейс** ([ADR-038](adr/ADR-038-move-chat-to-workspace.md)). Требуется хотя бы одно поле.

**Path:** `id` — UUID чата.

**Request:**
| Поле | Тип | Прим. |
|---|---|---|
| `title` | string, опц. | новый заголовок (≤ 200 символов) |
| `isPinned` | bool, опц. | закрепить/открепить |
| `workspaceProjectId` | string (uuid) \| null, опц. | **управление привязкой к воркспейсу** ([ADR-038](adr/ADR-038-move-chat-to-workspace.md)): `uuid` = перенести/сменить, `null` = убрать (станет обычным чатом). Поле отсутствует → привязка не трогается. Целевой workspace должен принадлежать пользователю → иначе `404 workspace_not_found` (как `/chat/run`). Идемпотентно. После переноса `instructions` проекта применяются со следующего сообщения; файлы-знания ретроспективно НЕ подмешиваются ([ADR-038 §3](adr/ADR-038-move-chat-to-workspace.md), [Q-038-1](99-open-questions.md)) |

**Response (200):** `{ "id": "uuid", "title": string|null, "isPinned": bool, "workspaceProjectId": "uuid"|null, "updatedAt": "ISO8601" }`.

**Коды:** `200`; `401`; `404` (чат / целевой workspace `workspace_not_found`); `422` (ни одного поля / `title` > 200); `429`; `5xx`.

> `workspaceProjectId` в `/chat/run` остаётся **session-fixed** (на resume игнорируется); изменить привязку существующего чата можно **только** этим `PATCH` — единый путь записи ([ADR-038 §4](adr/ADR-038-move-chat-to-workspace.md)).

### DELETE /v1/chats/{id}
Удаление чата (каскадно — шаги и tool-calls). Повторное удаление уже удалённого → `404`.

**Path:** `id` — UUID чата.
**Response (200):** `{ "deleted": true }`.
**Коды:** `200`; `401`; `404`; `429`; `5xx`.

---

## 18. Profile

Профиль пользователя: редактируемое `displayName` + производный `accountId`. Скоуп — `sub` JWT.

**Заголовки:** `Authorization: Bearer <JWT>`.

### GET /v1/profile
**Response (200):**
| Поле | Тип | Прим. |
|---|---|---|
| `accountId` | string | человекочитаемый идентификатор, производная от `userId`; формат `XXXX-XXXX-XXXXX` (две 4-значные цифровые группы + 5-символьная alphanumeric группа из алфавита `ABCDEFGHJKLMNPQRSTUVWXYZ23456789`, напр. `8472-1936-AXQ5K`). Детерминирован и стабилен; **не** секрет и **не** ключ авторизации (авторизация всегда по JWT `sub`) |
| `displayName` | string \| null | отображаемое имя (или `null`, если не задано) |
| `createdAt` | string (ISO8601) | дата создания аккаунта |

**Коды:** `200`; `401`; `429`; `5xx`.

### PATCH /v1/profile
**Request:**
| Поле | Тип | Прим. |
|---|---|---|
| `displayName` | string | новое имя (≤ 80 символов); пустая строка → сброс в `null` |

**Response (200):** тот же объект, что у `GET /v1/profile`, с обновлённым `displayName`.

**Коды:** `200`; `401`; `422` (`displayName` > 80 / схема); `429`; `5xx`.

---

## 19. Preferences

Пользовательские настройки. Источник дефолта `assistantMode` и `dialogMode` для `/chat/run` ([ADR-012](adr/ADR-012-assistant-mode-vs-billing-mode.md)/[ADR-055](adr/ADR-055-dialog-mode.md)). Если строка ещё не создана — возвращаются дефолты (`chat` / `smart` / `false` / `{}`). Дефолт `notificationsEnabled=false` ([ADR-032](adr/ADR-032-notifications-enabled-default-false.md)): privacy-by-default; iOS включает push через `PATCH` после системного разрешения. Существующие строки `user_preferences` сохраняют ранее сохранённое значение (без backfill).

**Заголовки:** `Authorization: Bearer <JWT>`.

### GET /v1/preferences
**Response (200):**
| Поле | Тип | Прим. |
|---|---|---|
| `defaultAssistantMode` | `chat` \| `code` | дефолтный тип ассистента; ортогонален billing_mode |
| `defaultDialogMode` | `smart` \| `deep_thinking` \| `study_learn` \| `search` | **дефолтный режим диалога** ([ADR-055](adr/ADR-055-dialog-mode.md)); используется при создании сессии, если `dialogMode` не передан в `/chat/run`. Дефолт `smart` при отсутствии строки. Ортогонален `assistantMode`/billing. |
| `notificationsEnabled` | bool | единый toggle уведомлений (push-токены — модуль notifications, Спринт 3); дефолт `false` при отсутствии строки ([ADR-032](adr/ADR-032-notifications-enabled-default-false.md)) |
| `codeDefaults` | object | дефолты Code-контекста (язык и т.п.); без секретов |

**Коды:** `200`; `401`; `429`; `5xx`.

### PATCH /v1/preferences
Частичное обновление (любое подмножество полей); создаёт строку при отсутствии (upsert). Требуется хотя бы одно поле.

**Request:** любое подмножество `{ "defaultAssistantMode": "chat"|"code", "defaultDialogMode": "smart"|"deep_thinking"|"study_learn"|"search", "notificationsEnabled": bool, "codeDefaults": object }`. `codeDefaults` — ≤ 8 KB сериализованного JSON, без секретов (ключи вида `key`/`token`/`secret` → `422`).

**Response (200):** полный актуальный объект настроек (как у `GET`).

**Коды:** `200`; `401`; `422` (ни одного поля / `codeDefaults` > 8 KB / секреты в `codeDefaults` / схема); `429`; `5xx`.

---

## 20. Tokens

Разовая покупка пакетов токенов через **consumable StoreKit IAP** → начисление кредитов на баланс ([ADR-015](adr/ADR-015-consumable-token-iap.md)). Отдельно от подписки (`/v1/subscription/sync` — auto-renewable). Тег `Tokens`. Скоуп — `sub` JWT.

**Заголовки:** `Authorization: Bearer <JWT>` (обязателен), `Content-Type: application/json`.

> ✅ **Требует активной подписки ([Q-015-1](99-open-questions.md) Closed = вариант B, 2026-06-02):** покупка токенов доступна **только подписчикам** (`subscription.status == active`) — это докупка кредитов сверх месячного пакета. Без активной подписки `POST /v1/tokens/purchase` возвращает `403` с `code=subscription_required`. PM/iOS: показывать покупку токенов только активным подписчикам; для неподписанных — CTA на оформление подписки.

### POST /v1/tokens/purchase
Обработка consumable-покупки пакета токенов: верификация StoreKit-транзакции (сервер не доверяет клиенту), маппинг `productId → credits` (server-side), идемпотентное начисление кредитов.

**Request:**
| Поле | Тип | Прим. |
|---|---|---|
| `userId` | string (uuid) | = `sub` JWT |
| `transaction` | object | подписанный StoreKit **consumable** payload (JWS / App Store Server API). **Не логируется** (redaction) |

**Поведение:** сначала сервер проверяет **активную подписку** пользователя ([Q-015-1](99-open-questions.md) = вариант B) — нет активной подписки → `403 subscription_required`, начисление не происходит. Затем верифицирует транзакцию (общий с subscription verifier, включая `STOREKIT_TEST_MODE` для e2e), извлекает `transactionId` и `productId`, разрешает число кредитов через server-side маппинг `TOKEN_PRODUCTS` (клиент не задаёт количество кредитов) и начисляет их как `credit`-транзакцию идемпотентно по `transactionId` (`meta.source=token_purchase`). Повтор той же транзакции не начисляет повторно.

**Response (200):**
| Поле | Тип | Прим. |
|---|---|---|
| `creditsAdded` | int | начислено кредитов за эту покупку; `0` при идемпотентном повторе (уже обработанная транзакция) |
| `newBalance` | int | баланс после начисления |
| `transactionId` | string | идентификатор обработанной StoreKit-транзакции |

**Коды:** `200`; `401` (нет/невалидный JWT); `403` — два случая: `code=subscription_required` (**нет активной подписки**, [Q-015-1](99-open-questions.md) вариант B) или `code=forbidden` (`userId ≠ sub`); `422` (невалидная/поддельная транзакция — кредиты не начисляются; либо неизвестный `productId` вне `TOKEN_PRODUCTS`); `429` (rate limit); `502/5xx` (ошибка App Store API / внутренняя). Примечание: `403 subscription_required` — это error-ответ операции пополнения, **не** `200+blocked` (правило [ADR-004](adr/ADR-004-blocked-http-200.md) действует только для эндпоинтов генерации/политики).

### GET /v1/tokens/products
Каталог доступных пакетов токенов (`productId → credits`). Цены отображает клиент из StoreKit; backend отдаёт только число кредитов на пакет.

**Response (200):**
```json
{ "products": [
    { "productId": "tokens_1500", "credits": 1500 },
    { "productId": "tokens_600",  "credits": 600 },
    { "productId": "tokens_250",  "credits": 250 },
    { "productId": "tokens_100",  "credits": 100 }
] }
```
Источник — server-side `TOKEN_PRODUCTS` (env/config, [07-deployment.md](07-deployment.md)). Состав/числа конфигурируемы и должны совпадать с заведёнными в App Store Connect IAP.

**Коды:** `200`; `401`; `429`; `5xx`.

---

## 12. blockReason — справочник

Когда `status="blocked"` (HTTP `200`), поле `blockReason` принимает одно из 10 значений. Что показывать в UI:

| blockReason | Значение | Что показать пользователю в UI |
|---|---|---|
| `trial_used` | Бесплатная попытка (1 lifetime) уже израсходована, подписки нет | Предложить оформить подписку |
| `subscription_required` | Действие требует подписки, её нет | CTA на покупку подписки |
| `subscription_expired` | Подписка истекла | Предложить продлить подписку |
| `credits_empty` | Кредиты закончились (есть подписка/режим credits) | Сообщить об исчерпании кредитов; предложить дождаться следующего периода или продлить |
| `byok_disabled` | Режим byok выбран, но BYOK не включён | Подсказать включить BYOK или добавить ключ в настройках |
| `byok_invalid` | BYOK-ключ невалиден (отклонён провайдером ключа — Anthropic/OpenAI) | Попросить обновить/перепроверить API-ключ |
| `rate_limited` | Слишком много запросов (транспортный лимит) | «Слишком часто, попробуйте позже». Приходит как HTTP `429` на gateway-уровне; **не** входит в `policy/effective.reasons[]` |
| `policy_denied` | Запрет по политике (общий fallback) | Общее «Действие сейчас недоступно» |
| `max_tokens` | Ответ Claude обрезан лимитом output-токенов ([ADR-025](adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)) | «Ответ слишком длинный — повторите или сократите запрос». **Отличие от прочих причин:** срабатывает **после** начала генерации — `usage`/`messageStepId`/`stepId` присутствуют; **кредит не списан**; инструменты (обрезанные) не отдаются. Не входит в `policy/effective.reasons[]`. |
| `image_credits_empty` | Кончились кредиты на генерацию изображения ([ADR-058](adr/ADR-058-image-generation.md)) — **в ЛЮБОМ режиме, включая `byok` и trial** (изображение оплачивается кредитами независимо от `mode`, [ADR-058 §4](adr/ADR-058-image-generation.md)) | «Недостаточно кредитов для генерации изображения». Отдельный дебет `IMAGE_CREDITS_COST` не прошёл: байты **не сохранены**, `rollback`. Отличается от `credits_empty` (нехватка на само сообщение) — профильный CTA. Не входит в `policy/effective.reasons[]`. |

> `reasons[]` в `GET /v1/policy/effective` содержит подмножество policy-причин (без `rate_limited` и без `max_tokens` — обе не являются предсказуемой до-генерационной policy-причиной) — для предварительной отрисовки доступности режимов в UI.

---

## 13. Tool-протокол

Чат работает по протоколу tool-use. Инструменты делятся на классы по исполнителю ([ADR-011](adr/ADR-011-server-side-tools.md), [ADR-026](adr/ADR-026-global-server-side-tools-and-time-now.md)): server-side project-scoped (`site.*`, требует проекта) и server-side global (`time.now`/`quiz.generate`/`image.generate`, без проекта). Класс **client-side** (исполнял iOS) с [ADR-063](adr/ADR-063-remove-client-side-calendar-reminders-files-tools.md) **не содержит инструментов**.

### Client-side (исполняет iOS) — инструментов нет ([ADR-063](adr/ADR-063-remove-client-side-calendar-reminders-files-tools.md))
Семейства `files.*`, `calendar.*`, `reminders.*` **удалены из сервиса** ([ADR-063](adr/ADR-063-remove-client-side-calendar-reminders-files-tools.md), решение владельца). Модель их не вызывает, `GET /v1/tools` не перечисляет, iOS не реализует. Протокол client-side (`status="tool_call"` → `POST /v1/chat/tool-result`, `toolCalls[]`, барьер хода) сохранён в контракте для обратной совместимости и будущих инструментов, но в штатном потоке не активируется (нет зарегистрированных client-side инструментов).

### Server-side, project-scoped (исполняет backend, требует проекта)
Инструменты `site.*` (website-builder) — backend исполняет немедленно внутри tool-loop и продолжает диалог с Claude **без** round-trip к iOS. Наружу как `tool_call` **не** отдаются. Предлагаются Claude **только** при наличии `projectId` ([ADR-022](adr/ADR-022-optional-project-and-tool-gating.md)). См. [раздел 10A](#10-website-builder--preview).

### Server-side, global (исполняет backend, без проекта) — [ADR-026](adr/ADR-026-global-server-side-tools-and-time-now.md)
| Инструмент | Тип | Назначение |
|---|---|---|
| `time.now` | read | текущая дата/время. Args: `{ "tz"?: "<IANA, напр. Europe/Moscow>" }`. Result: всегда `{ utc, unix, weekday }`; при валидном `tz` — дополнительно `{ local, timezone }`. Невалидный `tz` → tool-result error `invalid_timezone` (ход не падает). |
| `quiz.generate` | read | генерация квиза-**пула** (**только** при `dialogMode=study_learn`, [ADR-057](adr/ADR-057-study-learn-quiz-contract.md) + редизайн [ADR-062](adr/ADR-062-study-learn-quiz-pool.md)). `strict`-схема args `{ questions: [ { question, options[2..10], correctIndex, explanation } ] }` — **3..10** вопросов в одном вызове (все поля required; per-вопрос `correctIndex < len(options)`). «Исполнение» = валидация + эхо-возврат; результат (весь пул) поднимается в `ChatResponse.quiz` (`quiz.questions[]`), last-wins по пулу. При наличии квиза `assistantMessage` детерминированно `null` (без дубля вопросов/спойлера ответа, [ADR-062 §3](adr/ADR-062-study-learn-quiz-pool.md)). Результат считается на клиенте (эндпоинта проверки нет). Невалидный пул → graceful degrade all-or-nothing (`invalid_quiz` tool_result, модель чинит в ходе). Не отдаётся как `tool_call`. |
| `image.generate` | mutate | генерация изображения (OpenAI `gpt-image-1`, [ADR-058](adr/ADR-058-image-generation.md)). Байты сохраняются в `generated_images`, в ответ идёт `images[].imageId` (без промпта). **Отдельный дебет `IMAGE_CREDITS_COST` — списывается и при `credits`, и при `byok`** (наш серверный ключ; BYOK не освобождает, [ADR-058 §4](adr/ADR-058-image-generation.md)); идемпотентно по `tool_call_id`; нехватка → `blocked`/`image_credits_empty`. Ошибки (content-policy / сбой генерации / невалидный ключ) → tool-error, ход не падает; image-дебет при сбое не списывается. **Предлагается, если задан `OPENAI_API_KEY`** (не по `LLM_PROVIDER`). |

`time.now` исполняет backend в tool-loop (как `site.*`), но **без проекта** — предлагается Claude **всегда** (в т.ч. в «чистом чате»). Наружу как `tool_call` не отдаётся. Решает кейс «модель не знает текущую дату» — системный промт статичен (даты не несёт), модель получает время только из результата `time.now`. Read-only, без audit-мутации, без дополнительных списаний (1 кредит = 1 сообщение).

### Формат
- **tool_call** (от backend к iOS): `toolCalls = [ { "id": "<uuid>", "name": "<доменное имя>", "args": { ... } }, ... ]` — **все** client-side вызовы хода ([ADR-025](adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)). `id` — публичный стабильный идентификатор для `/chat/tool-result`. Поле `toolCall` (одиночное) = `toolCalls[0]`, **deprecated** — читайте `toolCalls[]`. С [ADR-063](adr/ADR-063-remove-client-side-calendar-reminders-files-tools.md) client-side инструментов нет — этот формат в штатном потоке не возникает (сохранён в контракте).
- **tool-result** (от iOS к backend, батч): `{ "userId", "sessionId", "results": [ { "toolCallId": "<id>", "result": { ... } }, { "toolCallId": "<id>", "error": { "code", "message" } } ] }`. В каждом элементе ровно одно из `result`/`error`. Backend продолжает диалог только когда собраны результаты на **все** `toolCalls[]` хода (барьер хода). Одиночная форма (`toolCallId` + `result|error` на верхнем уровне) — deprecated, поддерживается.
- Имена инструментов в публичном контракте — доменные, с точкой (`site.write_file`). Внутреннее преобразование к формату провайдера — деталь реализации, iOS её не касается.

---

## 14. Монетизация (кратко)

- **Trial:** ровно **1 бесплатное сообщение lifetime** на пользователя (флаг `trial_used`). Израсходован → `trial_used`.
- **Подписка:** при активации/продлении периода начисляется **фиксированный пакет кредитов** (`SUBSCRIPTION_CREDITS_PER_PERIOD`, дефолт **1000**) на баланс. Кредиты накапливаются между периодами (на старте не сгорают).
- **Покупка токенов (consumable IAP, [раздел 20](#20-tokens)):** второй источник пополнения баланса — разовая покупка пакета токенов начисляет кредиты ([ADR-015](adr/ADR-015-consumable-token-iap.md)). Покупка токенов **требует активной подписки** ([Q-015-1](99-open-questions.md) Closed = вариант B): без активной подписки запрос отклоняется → `403 subscription_required` (policy-guard до grant, [ADR-002](adr/ADR-002-access-policy-state-machine.md)). См. [раздел 20](#20-tokens).
- **Стоимость:** **1 кредит = 1 завершённое сообщение** (финальный `assistant_message`). Промежуточные tool-раунды не тарифицируются. Длина ответа на стоимость не влияет.
- **Режимы генерации (`mode`):**
  - `credits` — списывается кредит с баланса, генерация на сервисном Anthropic-ключе.
  - `byok` — пользователь приносит свой ключ (Anthropic или OpenAI, [ADR-044](adr/ADR-044-multi-provider-byok.md)); генерация через провайдера ключа; кредиты не списываются.

---

## 15. Превью сайта для iOS

Как iOS открывает сгенерированный Claude сайт:

1. В ходе чата (`/v1/chat/run`) Claude вызывает server-side инструмент `site.write_file` (backend сохраняет файлы) и затем `site.preview`.
2. Backend генерирует **signed URL** вида `GET /v1/preview/{projectId}/{token}/{entry}` (по умолчанию `entry=index.html`) с TTL (дефолт **15 минут**). `site.preview` возвращает его как **абсолютный** URL `https://<SERVICE_DOMAIN>/v1/preview/...` ([ADR-031](adr/ADR-031-absolute-preview-url.md)) — модель копирует ссылку дословно, не достраивая хост. URL доходит до пользователя как часть ответа Claude.
3. iOS открывает этот URL **напрямую** в браузере / `WKWebView` — без передачи JWT и без cookies. Авторизация целиком в подписи URL.
4. Контент отдаётся в **sandbox** (CSP `sandbox`, `X-Frame-Options: SAMEORIGIN`, `nosniff`, `no-store`), без cookies/credentials — пользовательский JS изолирован от API-origin.
5. После истечения TTL ссылка перестаёт работать (`403`) — нужно запросить новую через `site.preview` (новый chat-шаг).

---

## 16. Лимиты и rate limits

### Размер payload
| Лимит | Значение | Нарушение |
|---|---|---|
| Общий размер тела запроса (все роуты, кроме upload-роутов ниже) | ≤ 512 KB | `413` |
| Тело `/v1/chat/run` (повышенный — под inline base64-вложения, [ADR-020](adr/ADR-020-inline-base64-attachments-mvp.md)) | ≤ 12 MB | `413` |
| Тело `POST /v1/workspaces/{id}/files` (повышенный — под inline base64 workspace-файлов, [ADR-045](adr/ADR-045-per-path-body-limit-workspace-files.md)) | ≤ 12 MB | `413` |
| `message` (`/chat/run`) | ≤ 32 KB; опц. при ≥1 attachment ([ADR-039](adr/ADR-039-optional-message-with-attachments.md)), иначе пустой → `422` | `422` |
| `context` (`/chat/run`) | ≤ 64 KB | `422` |
| `attachments[]` (`/chat/run`, [ADR-020](adr/ADR-020-inline-base64-attachments-mvp.md)) | ≤ 10 шт.; одно ≤ 5 MB image / 8 MB document; суммарно ≤ 10 MB; PDF ≤ 100 стр. | `413`/`422` |
| `result` (`/chat/tool-result`) | ≤ 256 KB | `422` |
| `apiKey` (BYOK) | ≤ 4 KB | `422` |
| Тело admin-запроса | ≤ 8 KB | `413`/`422` |

### Rate limits (дефолты; калибруются на проде)
| Эндпоинт | Лимиты |
|---|---|
| `POST /v1/chat/run` | 30 req/min per user · 60 req/min per device · 120 req/min per IP |
| Прочие POST `/v1/*` | 60 req/min per user |
| `/v1/admin/*` | 10 req/min per source IP |

Превышение → `429` (с телом `{ error.code: "rate_limited" }`).

### Лимиты website-builder
| Лимит | Значение |
|---|---|
| Размер одного файла сайта | ≤ 1 MB |
| Суммарный размер проекта | ≤ 10 MB |
| Число файлов в проекте | ≤ 200 |
| TTL preview signed URL | 15 минут (дефолт) |

Все значения — конфигурируемые дефолты из server-side настроек; на проде могут быть откалиброваны.

---

## 21. Auth (выпуск токена)

Встроенный issuer ([ADR-018](adr/ADR-018-embedded-auth-issuer.md), [modules/auth](modules/auth/README.md)): backend САМ выпускает и верифицирует RS256 JWT. Эндпоинты `/v1/auth/*` — **без** пользовательского JWT (точка его получения); защита — per-IP rate-limit. Первичная аутентификация — **device-based** (анонимная) + **Sign in with Apple** для кросс-девайс аккаунта ([ADR-043](adr/ADR-043-sign-in-with-apple.md), закрывает [Q-018-2](99-open-questions.md)). Email/пароль — опциональное расширение, не MVP.

### POST /v1/auth/register
Создать/найти идентичность устройства и выдать токены.
**Заголовки:** `Content-Type: application/json` (без `Authorization`).
**Request:** `{ "deviceId": "string (опц.)" }` — если не передан/пуст, backend сгенерирует UUIDv4.
**Response 200:**
```json
{ "userId": "uuid", "deviceId": "string", "accessToken": "<JWT>", "tokenType": "Bearer",
  "expiresIn": 3600, "refreshToken": "<opaque>", "refreshExpiresIn": 2592000 }
```
- Известный `deviceId` → возвращается тот же `userId` (идемпотентно). Новый → новый `userId` + provisioning.
**Коды:** `200`; `422` невалидный `deviceId`; `429` rate-limit; `503` issuer не сконфигурирован (нет приватного ключа).

### POST /v1/auth/token
Токены для **уже зарегистрированного** устройства. **Request:** `{ "deviceId": "string" }` (обязателен). Ответ — как у register. **Коды:** `200`; `422`; `429`; `503`.

### POST /v1/auth/refresh
Обмен refresh-token на новую пару (single-use rotation). **Request:** `{ "refreshToken": "<opaque>" }`. Ответ — новая пара (тот же `userId`). Reuse использованного/истёкшего → `401` + ревокация цепочки устройства. **Коды:** `200`; `401`; `422`; `429`.

### POST /v1/auth/apple
Sign in with Apple ([ADR-043](adr/ADR-043-sign-in-with-apple.md)) — кросс-девайс аккаунт.
**Заголовки:** `Content-Type: application/json` (без `Authorization`).
**Request:** `{ "identityToken": "<Apple OIDC JWT>", "deviceId": "string (опц.)", "nonce": "string (опц.)" }` — Apple identity token (нативный Sign in with Apple, RS256). `deviceId` опционален (как register). `nonce` — raw-nonce, переданный Apple (проверяется при наличии claim).
**Response 200:** `TokenResponse` (как register — НАША пара токенов).
- Apple-аккаунт известен → тот же `userId` (кросс-девайс). Неизвестен → привязка к device-аккаунту без Apple-идентичности (сохраняет кредиты/историю) или новый пользователь. Конфликт → берём Apple-аккаунт, без авто-merge данных.
**Верификация:** `iss=https://appleid.apple.com`, `aud`=bundle id, RS256 по Apple JWKS, claims `sub`/`iss`/`aud`/`exp`. Токен/nonce не логируются.
**Коды:** `200`; `401` невалидный/просроченный/неверный `iss`/`aud`/подпись/nonce; `422` нарушение схемы; `429` rate-limit; `503` issuer не сконфигурирован или Apple-аудитория не задана.

### GET /v1/auth/jwks
JWKS с публичным ключом (для самопроверки/отладки). Опционально-публичный (`AUTH_JWKS_ENABLED`, дефолт `true`); приватный ключ никогда не отдаётся. **Коды:** `200`; `404` (выключен/не сконфигурирован).

### Токены (кратко)
- **Access-token:** RS256 JWT, TTL 1ч; claims `sub`/`device_id`/`iss`/`aud`/`iat`/`exp`, заголовок `kid`. Используется как `Authorization: Bearer` для всех `/v1/*`.
- **Refresh-token:** opaque, TTL 30д, single-use rotation, хранится как хэш (серверная ревокация при logout/краже).

---

## 22. Tools (каталог инструментов)

### GET /v1/tools
Машиночитаемый каталог всех поддерживаемых backend tools — **8** (все server-side; client-side `files.*`/`calendar.*`/`reminders.*` удалены, [ADR-063](adr/ADR-063-remove-client-side-calendar-reminders-files-tools.md)): `site.write_file/preview/list/read/delete`, `time.now`, `quiz.generate` [ADR-057](adr/ADR-057-study-learn-quiz-contract.md), `image.generate` [ADR-058](adr/ADR-058-image-generation.md). Видимость server-side инструмента в каталоге ≠ его вызываемость клиентом (server-side исполняет backend, в `toolCalls[]` не отдаются) и ≠ доступность в текущем ходе (dialog-gate `quiz.generate` — только `study_learn`; key-gate `image.generate` — только если задан `OPENAI_API_KEY`, независимо от `LLM_PROVIDER`). [ADR-019](adr/ADR-019-tools-catalog-endpoint.md), [ADR-026](adr/ADR-026-global-server-side-tools-and-time-now.md), [chat-orchestrator/02-api-contracts](modules/chat-orchestrator/02-api-contracts.md#get-v1tools--каталог-инструментов-adr-019).
**Заголовки:** `Authorization: Bearer <JWT>` (обязателен — как все `/v1/*`; каталог не секретен, но контур единый).
**Response 200:**
```json
{ "tools": [ { "name": "site.write_file", "description": "...", "mutating": true,
  "execution": "server", "inputSchema": { "type": "object", "properties": { } } } ] }
```
- `name` — доменное имя с точкой. `mutating` (bool) — требует ли audit. `execution` — `"client"` (исполняет iOS; сейчас таких нет, [ADR-063](adr/ADR-063-remove-client-side-calendar-reminders-files-tools.md)) или `"server"` (`site.*` — [ADR-011](adr/ADR-011-server-side-tools.md); `time.now` — [ADR-026](adr/ADR-026-global-server-side-tools-and-time-now.md); исполняет backend). `inputSchema` — JSON Schema args.
- Возвращает **полный** реестр (не фильтруется по `assistantMode`/проекту). Список из 8: `site.write_file/preview/list/read/delete` (server, project-scoped), `time.now`/`quiz.generate`/`image.generate` (server, global).
**Коды:** `200`; `401`; `429`.

---

## 24. Models (список моделей инстанса)

### GET /v1/models
Список моделей, доступных для выбора на **этом** инстансе (модели активного LLM-провайдера). Источник для селектора модели в композере iOS. [ADR-034](adr/ADR-034-user-model-selection.md), [chat-orchestrator/02-api-contracts](modules/chat-orchestrator/02-api-contracts.md#get-v1models--список-доступных-моделей-инстанса-adr-034).
**Заголовки:** `Authorization: Bearer <JWT>` (обязателен — как все `/v1/*`).
**Response 200:**
```json
{ "models": [
  { "id": "gpt-4o", "displayName": "GPT-4o", "default": true },
  { "id": "gpt-4o-mini", "displayName": "GPT-4o mini", "default": false }
] }
```
- `id` — provider-id модели; отправляется обратно в `POST /v1/chat/run` `model`. `displayName` — для UI. `default` (bool) — ровно одна модель `true` (дефолтная модель инстанса `ANTHROPIC_MODEL`/`OPENAI_MODEL`), идёт первой.
- Набор — allowlist активного провайдера (`ANTHROPIC_MODELS`/`OPENAI_MODELS`, выбор по `LLM_PROVIDER`). **Пустой allowlist** ⇒ ровно один элемент = дефолтная модель инстанса (обратная совместимость).
- Контракт провайдер-агностичен; на anthropic-инстансе — Claude-модели, на openai — OpenAI-модели. Выбрать модель чужого провайдера нельзя ([ADR-034 §7](adr/ADR-034-user-model-selection.md)).
**Коды:** `200`; `401`; `429`.

---

## 25. Presets (пресеты промтов)

### GET /v1/presets
Пресеты промтов для чипов на главном экране чата (экран 4). Тап подставляет `prompt` в композер. Набор/тексты меняются деплоем backend **без релиза iOS-приложения**. [ADR-035](adr/ADR-035-prompt-presets-endpoint.md), [ADR-049](adr/ADR-049-presets-localization.md) (локализация), [chat-orchestrator/02-api-contracts](modules/chat-orchestrator/02-api-contracts.md#get-v1presets--пресеты-промтов-adr-035).
**Заголовки:** `Authorization: Bearer <JWT>` (обязателен — как все `/v1/*`; каталог не секретен, контур единый). Опц. `Accept-Language` (см. резолвинг локали).
**Query:** `locale` (опц., набор `en`/`ru`) — явный выбор локали; вне набора → `422`.
**Резолвинг локали ([ADR-049](adr/ADR-049-presets-localization.md)):** `?locale=` → `Accept-Language` (первый поддерживаемый, `ru-RU`→`ru`) → per-instance `PRESETS_DEFAULT_LOCALE` (avelyra=`ru`, остальные=`en`) → `en`.
**Response 200:**
```json
{ "locale": "ru", "presets": [
  { "id": "plan_week", "title": "Планирование недели", "icon": "calendar",
    "prompt": "Помоги спланировать предстоящую неделю. ..." }
] }
```
- `locale` — фактически отданная локаль (аддитивно). `id` — стабильный slug (snake_case, **не локализуется**). `title` — имя чипа (на локали). `icon` — имя **SF Symbol** (не emoji; клиент рендерит `Image(systemName:)`, fallback при отсутствии; **не локализуется**). `prompt` — текст в композер (на локали). Порядок = порядок чипов (един во всех локалях); все поля пресета обязательны.
- Дефолтный набор (7): `plan_week`, `meeting_notes`, `tasks_from_photo`, `design_brief`, `daily_review`, `summarize_text`, `project_structure`.
- Провайдер/инстанс-агностично; локализуются только `title`/`prompt`, EN — канон/fallback (per-field). Без БД/миграции/биллинга; read-only без побочных эффектов. Без env/без запроса локали → EN как раньше (обратная совместимость; [Q-035-2](99-open-questions.md) частично закрыт [ADR-049](adr/ADR-049-presets-localization.md)).
**Коды:** `200`; `401`; `422` (явный `?locale=` вне набора); `429`.

---

## 26. Workspaces (рабочие пространства / «Projects») ([ADR-036](adr/ADR-036-workspaces-implementation.md))

Рабочее пространство = `name` + `description` + кастомные `instructions` (system-prompt проекта) + файлы-знания (контекст для всех чатов проекта) + группировка чатов. iOS отображает «Projects»; API-путь — **`/v1/workspaces`** (слово «project» в API занято website-builder, [ADR-013](adr/ADR-013-workspace-projects-vs-website-builder.md)). Все эндпоинты — JWT, изоляция по `sub`: чужой/несуществующий → `404`. Биллинг: CRUD/файлы бесплатны; генерация в чате проекта — 1 кредит ([ADR-006](adr/ADR-006-credit-billing-and-subscription-grant.md)). Модуль — [modules/workspaces](modules/workspaces/README.md).

### POST /v1/workspaces — создать
**Request:** `{ "name": "string", "description": "string?", "instructions": "string?" }` (`name` ≤ 120, `description` ≤ 1000, `instructions` ≤ 16000).
**Response 201:** `{ id, name, description|null, instructions|null, createdAt, updatedAt }`.

### GET /v1/workspaces — список (курсорная пагинация)
**Query:** `cursor` (opaque, опц.), `limit` (1..100, дефолт 50). Порядок — `updatedAt DESC`.
**Response 200:** `{ "items": [ { id, name, description|null, updatedAt, fileCount, chatCount } ], "nextCursor": string|null }`.

### GET /v1/workspaces/{workspace_id} — полный объект
**Response 200:** `{ id, name, description|null, instructions|null, files: [ { fileId, filename, mediaType, size, hasExtractedText, createdAt } ], createdAt, updatedAt }`. Тело файлов (`content`/`extractedText`) в API не отдаётся.

### PATCH /v1/workspaces/{workspace_id} — обновить
**Request:** `{ name?, description?, instructions? }` (хотя бы одно поле; `description`/`instructions` можно очистить `null`). **Response 200:** полный объект (как GET /{workspace_id}).

### DELETE /v1/workspaces/{workspace_id}
**Response 200:** `{ "deleted": true }`. Файлы-знания удаляются CASCADE; чаты проекта **сохраняются** (`workspace_project_id` → `null`).

### POST /v1/workspaces/{workspace_id}/files — загрузить файл-знание (inline base64)
**Request:** `{ "type": "image|document|text", "mediaType": "string", "filename": "string", "data": "base64" }` — те же классы/allowlist/валидации, что у chat-вложений ([ADR-020](adr/ADR-020-inline-base64-attachments-mvp.md)). Backend извлекает `extracted_text` (pypdf для PDF, decode для текста), сохраняет байты в БД (BYTEA, [TD-027](100-known-tech-debt.md)).
Лимиты: ≤ 20 файлов/workspace; файл ≤ 8 MB; суммарно ≤ 32 MB. Вне allowlist → `422`; превышение размера → `413`; превышение числа/суммы → `422`. **Transport body-limit повышен до 12 MB** для этого роута (`WORKSPACE_REQUEST_BODY_LIMIT`, [ADR-045](adr/ADR-045-per-path-body-limit-workspace-files.md)) — иначе base64 8 MB-файла резался бы общим 512 KB в gateway.
**Response 201:** `{ fileId, filename, mediaType, size, hasExtractedText, createdAt }`.

### GET /v1/workspaces/{workspace_id}/files — список файлов
**Response 200:** `{ "items": [ { fileId, filename, mediaType, size, hasExtractedText, createdAt } ] }`.

### DELETE /v1/workspaces/{workspace_id}/files/{file_id}
**Response 200:** `{ "deleted": true }`. Отсутствующий/чужой → `404`. (Path-параметры URL — `workspace_id`/`file_id`; в теле ответов id-поля — camelCase: `fileId`.)

> **Подача контекста модели.** В сессии с `workspaceProjectId`: `instructions` → system-prompt (после base assistant_mode prompt) на **КАЖДОМ ходе** — turn 0 И continuation tool-loop (`system` не часть истории, переинъектируется на каждый вызов LLM, [ADR-036 §3](adr/ADR-036-workspaces-implementation.md)). Файлы-знания — только turn 0: document/text → `extracted_text` (работает на **обоих** провайдерах — это текст, не нативный PDF, ограничение [TD-023](100-known-tech-debt.md) не применяется); image → vision (сохраняются в истории, на continuation не дублируются). Лимит суммарного текста — `WORKSPACE_CONTEXT_MAX_CHARS` (усечение, [Q-013-1](99-open-questions.md)).

> **Список чатов проекта** — `GET /v1/chats?workspaceProjectId={id}` (раздел 17).

**Коды (общие для раздела):** `200`/`201`; `401`; `404` (чужой/несуществующий workspace или fileId); `413`/`422` (лимиты/валидация файлов); `429`; `5xx`.

---

## 27. Images (генерация изображений) ([ADR-058](adr/ADR-058-image-generation.md))

Изображения генерируются server-side инструментом `image.generate` (OpenAI `gpt-image-1`) в ходе `/v1/chat/run`; байты хранятся вне чата (таблица `generated_images`), в ответе генерации приходит `images[].imageId` (только id/`contentType`/`size`, без промпта — [ADR-058 §7](adr/ADR-058-image-generation.md)), а байты фетчатся отдельным endpoint `GET /v1/images/{imageId}`. Перечень всех своих изображений пользователь получает списком `GET /v1/images` (курсорная пагинация, без промтов и байтов — [ADR-058 §8](adr/ADR-058-image-generation.md)). Генератор — отдельный OpenAI-клиент, **независимый от `LLM_PROVIDER`**: `image.generate` активен на **любом** инстансе, где задан `OPENAI_API_KEY` (включая Anthropic-чат-инстанс с настроенным ключом), и не предлагается там, где ключ не задан ([ADR-058 §3](adr/ADR-058-image-generation.md)).

### GET /v1/images
Список всех изображений текущего пользователя (галерея), курсорная пагинация. ([ADR-058 §8](adr/ADR-058-image-generation.md))

**Заголовки:** `Authorization: Bearer <JWT>` (обязателен).

**Query-параметры:**
| Параметр | Тип | Прим. |
|---|---|---|
| `cursor` | string, опц. | непрозрачный (opaque) курсор следующей страницы — берётся из `nextCursor` предыдущего ответа |
| `limit` | int, опц. | размер страницы, 1..100 (дефолт 30) |

**Response (200):**
| Поле | Тип | Прим. |
|---|---|---|
| `items` | array | изображения текущей страницы, порядок `createdAt DESC` (новые первыми) |
| `items[].imageId` | string (uuid) | идентификатор изображения (для фетча `GET /v1/images/{imageId}`) |
| `items[].contentType` | string | MIME (`image/png`\|`image/jpeg`\|`image/webp`) |
| `items[].size` | int | размер в байтах |
| `items[].createdAt` | string (ISO8601) | время генерации |
| `items[].sessionId` | string (uuid) \| null | чат-сессия происхождения; `null` для изображения временного чата ([ADR-056](adr/ADR-056-temporary-chat.md)) |
| `nextCursor` | string \| null | курсор следующей страницы; `null`, если страниц больше нет |

**Приватность:** в элементе списка **нет `prompt`** (промт хранится server-side, наружу не отдаётся — [ADR-058 §7](adr/ADR-058-image-generation.md)/[TD-035](100-known-tech-debt.md)) и **нет байтов** (байты — только `GET /v1/images/{imageId}`).

**Изоляция/свежесть:** только изображения владельца (`generated_images.user_id == sub`); **истёкшие по TTL картинки в список не попадают** (условие `expires_at IS NULL OR expires_at > now()` в запросе — та же логическая недоступность, что в item-эндпоинте, [ADR-058 §6](adr/ADR-058-image-generation.md)). Чужое/истёкшее/несуществующее просто отсутствует в выдаче (не отдельный код).

**Коды:** `200` (в т.ч. пустой `items: []`); `401`; `422` (`limit` вне диапазона / битый `cursor`); `429`; `5xx`.

### GET /v1/images/{imageId}
Отдать байты сгенерированного изображения.

**Заголовки:** `Authorization: Bearer <JWT>` (обязателен).

**Response 200:** сырые байты изображения; `Content-Type` = `image/png`\|`image/jpeg`\|`image/webp`; security-заголовки `X-Content-Type-Options: nosniff`, `Cache-Control: private, no-store` (образец preview [ADR-010](adr/ADR-010-backend-hosted-preview.md)).

**Авторизация/изоляция:** доступ только владельцу (`generated_images.user_id == sub`). Чужое/несуществующее **или истёкшее по TTL** изображение → **`404`** (не `403` — не раскрываем существование, как `GET /v1/chats/{id}`/preview). Истечение проверяется по `expires_at` в запросе (изображения временного чата истекают по `TEMPORARY_IMAGE_TTL_SECONDS`, [ADR-058 §6](adr/ADR-058-image-generation.md)).

**Биллинг:** фетч бесплатен; списание `IMAGE_CREDITS_COST` происходит при генерации в `/chat/run` (отдельный дебет сверх биллинга хода, идемпотентно по `tool_call_id`). **Списывается независимо от `mode` — и при `credits`, и при `byok`** (изображение генерируется на нашем серверном `OPENAI_API_KEY`; BYOK покрывает только текст, [ADR-058 §4](adr/ADR-058-image-generation.md)). Trial: текст бесплатен, изображение требует ненулевого баланса. Нехватка кредитов на картинку (в любом режиме) → ход `blocked` + `blockReason=image_credits_empty` (байты не сохранены).

**Коды:** `200`; `401` (нет/невалидный JWT); `404` (чужое/несуществующее/истёкшее); `429`.

### DELETE /v1/images/{imageId}
Удалить своё сгенерированное изображение из галереи (`GET /v1/images`). Hard-delete: строка и её байты удаляются безвозвратно. ([ADR-058 §9](adr/ADR-058-image-generation.md))

**Заголовки:** `Authorization: Bearer <JWT>` (обязателен).

**Path:** `imageId` — UUID изображения.

**Response 204:** пустое тело (No Content); security-заголовки `X-Content-Type-Options: nosniff`, `Cache-Control: private, no-store`.

**Авторизация/изоляция:** удалить можно только своё изображение (`generated_images.user_id == sub`). Чужое / несуществующее / истёкшее по TTL / уже удалённое → **`404`** (не `403` — не раскрываем существование, как `GET /v1/images/{imageId}`). Повторное удаление того же изображения → **`404`** (а не `204`).

**Последствия:** ссылка на удалённое изображение может оставаться в истории чата (в шаге хранится только `id`, не байты) — последующий `GET /v1/images/{imageId}` по такому «висячему» id вернёт `404`. Переписку (историю чата) удаление изображения **не меняет**. Списанные за генерацию кредиты **не возвращаются**.

**Коды:** `204` (удалено); `401` (нет/невалидный JWT); `404` (чужое/несуществующее/истёкшее/повторное); `429`; `5xx`.

---

## 23. Как тестировать через Swagger

Интерактивная документация — `/docs` (Swagger UI) при `DOCS_ENABLED=true` (dev/staging; на `broadnova.shop` сейчас включена). Порядок ручной проверки:

1. **Получить токен.** Открой `POST /v1/auth/register` → «Try it out» → тело `{}` (или `{ "deviceId": "my-test-device" }`) → «Execute». В ответе скопируй `accessToken`.
   - Повторно для того же `deviceId` — `POST /v1/auth/token`. Обновить пару — `POST /v1/auth/refresh` с `refreshToken`.
   - Если ответ `503` — на сервере не сконфигурирован приватный ключ подписи (issuer выключен); см. [§21](#21-auth-выпуск-токена) и prod-checklist деплоя.
2. **Авторизоваться (`bearerAuth`).** Кнопка «Authorize» (вверху справа) → схема `bearerAuth` → вставь `accessToken` (без слова `Bearer`, Swagger добавит сам) → «Authorize». Теперь все `/v1/*` шлются с `Authorization: Bearer <JWT>`.
3. **Дёргать защищённые эндпоинты.** Любой `/v1/*` (например `GET /v1/tools`, `GET /v1/policy/effective`, `POST /v1/chat/run`). В теле, где есть `userId`, подставляй `userId` = `sub` из токена (иначе `403`).
4. **Admin-эндпоинты (`adminToken`).** Для `/v1/admin/*` — отдельная схема в «Authorize»: `adminToken` → вставь значение `ADMIN_API_SECRET` (уйдёт как заголовок `X-Admin-Token`). Пользовательский `bearerAuth` к admin-эндпоинтам не подходит — контуры изолированы.
5. **Preview** через Swagger не тестируется обычным способом — `GET /v1/preview/*` открывается прямой signed-URL ссылкой в браузере (авторизация в URL, не в заголовке).

> Если `/docs` отдаёт `404` — на этом окружении `DOCS_ENABLED=false` (рекомендация для prod). Тогда тестируй через `curl`/Postman по контрактам выше.

---

*Документ — внешний deliverable для PM/интеграторов. Источник истины по контрактам — `docs/modules/*/02-api-contracts.md` и `docs/05-security.md`; продуктовые правила — ADR-004 (blockReason), ADR-006 (монетизация), ADR-009 (admin), ADR-048 (admin-активация подписки), ADR-010 (preview), ADR-011 (server-side tools), ADR-018 (auth), ADR-019 (tools catalog).*
