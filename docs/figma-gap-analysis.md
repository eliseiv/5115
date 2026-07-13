# Figma Gap Analysis — дизайн iOS «Mythos Claude Code» ↔ backend

Дата: 2026-06-02. Источник: 15 экранов Figma (прочитаны main chat, выжимка передана architect). Это **расширение** реализованного backend, не bootstrap.

> **Статус расширения (2026-06-02):** **Спринт 1 реализован** (chats / profile / preferences / расширение BYOK-статусов / `assistantMode`). **Token Purchase реализован (MVP)** — перенесён из Спринта 3. **Встроенный auth-issuer реализован** (`/v1/auth/register|token|refresh`, `GET /v1/auth/jwks`, device-based, миграция `0005`) — **[Q-005-1](99-open-questions.md) закрыт реализацией**. **Каталог инструментов `GET /v1/tools` реализован** ([ADR-019](adr/ADR-019-tools-catalog-endpoint.md)). Offline-сьют **775/775 зелёный**, production-ready. **Спринты 2/3 (остаток: workspaces, snippets, attachments, notifications) — спроектированы, ожидают реализации** (код не написан). Открытые вопросы пользователя: **Q-015-1 — Closed (вариант B: покупка токенов требует активной подписки)**; Q-016-1, Q-016-2 — **ждут ответа пользователя**.

Легенда статуса:
- ✅ **Покрыто** — контракт/модель уже есть, изменений не требуется.
- 🟡 **Частично** — есть основа, нужно расширение контракта/модели.
- 🔴 **Пробел** — нужен новый модуль/контракт.

## MVP-scope (решение пользователя, 2026-06-02)

Зафиксирован объём первого публичного релиза (MVP). Это **продуктовое решение пользователя**, источник истины для приоритизации backend-работы.

### Входит в MVP (реализуется сейчас)
| Блок | Состав | Статус |
|---|---|---|
| **Базовый backend** | 9 базовых модулей (7 core + Admin + Website Builder), биллинг, policy, BYOK, tool-loop, preview | ✅ Реализован |
| **Admin** | `POST /v1/admin/wallet/grant`, `GET /v1/admin/wallet/{userId}` ([ADR-009](adr/ADR-009-admin-token-auth.md)) | ✅ Реализован |
| **Website Builder** (**опциональная** фича) | server-side `site.*` tools, backend-hosted preview ([ADR-010](adr/ADR-010-backend-hosted-preview.md)/[ADR-011](adr/ADR-011-server-side-tools.md)). Активна только при сессии с `projectId` ([ADR-022](adr/ADR-022-optional-project-and-tool-gating.md)); основной поток — чат-агрегатор без проекта | ✅ Реализован |
| **Спринт 1** | chats, profile, preferences, расширение BYOK-статусов ([ADR-016](adr/ADR-016-extended-byok-statuses.md)), `assistantMode` ([ADR-012](adr/ADR-012-assistant-mode-vs-billing-mode.md)) | ✅ Реализован |
| **Token Purchase** | consumable IAP → grant кредитов ([ADR-015](adr/ADR-015-consumable-token-iap.md)), перенесён из Спринта 3 в MVP. `POST /v1/tokens/purchase` + `GET /v1/tokens/products`, идемпотентность по `transactionId`, reuse StoreKit-verifier + `WalletService.grant`, без миграции | ✅ **Реализован (MVP)** — ⏳ доработка policy-guard «требует активной подписки» ([Q-015-1](99-open-questions.md) Closed = вариант B) |

### Post-MVP (после первого релиза)
| Блок | Состав | Причина отложения |
|---|---|---|
| **Спринт 2** | workspaces, snippets | Не критично для первого релиза |
| **Спринт 3 (остаток)** | двухшаговый upload-модуль `attachments` (таблица `attachments`, [TD-015](100-known-tech-debt.md)) — отложен; notifications (хранение токена + push → [TD-011](100-known-tech-debt.md)) | Не критично для первого релиза. **Мультимодальный ввод (chat-вложения) НЕ в этом блоке — реализован на MVP inline base64 ([ADR-020](adr/ADR-020-inline-base64-attachments-mvp.md)), см. строку 51.** |
| **Actions / Choose style** | пресеты ([Q-016-1](99-open-questions.md)) | Открытый вопрос; дефолт — клиентские пресеты, backend не нужен |
| **Web search** | server-side tool ([Q-016-2](99-open-questions.md)) | Блокер: не выбран провайдер поиска и тарификация |

> **Token Purchase перенесён в MVP и реализован (2026-06-02).** В таблице соответствия ниже модуль исторически помечен «Спринт 3», но по решению пользователя вошёл в MVP вместе с базой и Спринтом 1 и **реализован**: `POST /v1/tokens/purchase` (StoreKit consumable transaction + `productId` → `creditsAdded`/`newBalance`/`transactionId`), `GET /v1/tokens/products`, идемпотентность по `transactionId`, маппинг `productId→credits` server-side (`TOKEN_PRODUCTS`), reuse StoreKit-verifier + `WalletService.grant`, без миграции (`ledger.meta.source=token_purchase`). ТЗ модуля — [ADR-015](adr/ADR-015-consumable-token-iap.md) и [modules/token-purchase/](modules/token-purchase/README.md). [Q-015-1](99-open-questions.md) Closed = вариант B: покупка токенов **требует активной подписки** — без подписки запрос отклоняется `403 subscription_required` (policy-guard до grant), см. строку 109 / [ADR-015 §Доступность](adr/ADR-015-consumable-token-iap.md). **Остальные post-MVP-блоки (Спринты 2/3) — спроектированы, не реализованы.**

## Сводная таблица соответствия

| Дизайн-функция (экран) | Статус | Backend | Новые ADR | Спринт |
|---|---|---|---|---|
| Chat run (Chat/Code), tool-loop, permission, blockReason | ✅ | chat-orchestrator, policy-engine | — | — |
| Subscription sync, trial/credits | ✅ | subscription, wallet-ledger, ADR-006 | — | — |
| BYOK set/toggle/delete | ✅ (статусы 🟡) | byok | — | — |
| calendar.create_events tool + permission | ❌ удалён ([ADR-063](adr/ADR-063-remove-client-side-calendar-reminders-files-tools.md)) | — (client-side `calendar.*`/`reminders.*`/`files.*` убраны из сервиса решением владельца) | — | — |
| Site generation / preview | ✅ | website-builder, ADR-010/011 | — | — |
| **Chats list/search/rename/delete/pin, steps-view** | ✅ **реализовано** (Спринт 1) | **chats** поверх `chat_sessions`/`chat_steps` | ADR-012 (assistant_mode) | **1** |
| **Profile (displayName, accountId)** | ✅ **реализовано** (Спринт 1) | **profile**, `users.display_name` | — | **1** |
| **Preferences (default mode chat/code, Code-defaults, notif toggle)** | ✅ **реализовано** (Спринт 1) | **preferences**, `user_preferences` | ADR-012 | **1** |
| **Расширенные BYOK-статусы + активная модель** | ✅ **реализовано** (Спринт 1) | byok (enum 6 значений + `activeModel`) | ADR-016 | **1** |
| **assistant_mode (chat/code) vs billing_mode (credits/byok)** | ✅ **реализовано** (Спринт 1) | chat-orchestrator/preferences (`assistantMode` в `/chat/run`) | ADR-012 | **1** |
| **Projects-воркспейсы (name/desc/instructions/files/чаты)** | 🔴 | **workspaces** (новый), `workspace_projects`/`workspace_files` | ADR-013 | **2** |
| **Snippets (Code-режим)** | 🔴 | **snippets** (новый), `snippets` | — | **2** |
| **Мультимодальный ввод (фото/файлы → Claude vision)** | 🟢 реализован (MVP, 2026-06-03) | **inline base64 в `/chat/run`** (chat-orchestrator, без отдельного модуля/таблицы; `src/app/chat/attachments.py`); двухшаговый `attachments` отложен ([TD-015](100-known-tech-debt.md)); live-e2e PDF document-блока — после восстановления org Anthropic ([TD-016](100-known-tech-debt.md)) | **ADR-020** (заменяет транспорт ADR-014) | **MVP** |
| **Покупка токенов (consumable IAP)** | 🔴 | **token-purchase** (новый), reuse ledger/Wallet | ADR-015 | **3** |
| **Аутентификация / выпуск JWT (онбординг устройства)** | ✅ **реализовано** | **auth** (новый), `auth_devices`/`auth_refresh_tokens` (миграция `0005`) | ADR-018 (закрывает Q-005-1) | **—** |
| **Каталог инструментов (`GET /v1/tools`)** | ✅ **реализовано** | chat-orchestrator (`src/app/chat/tools.py`) | ADR-019 | **—** |
| **Notifications (toggle + push device-token)** | 🟡/🔴 | **notifications** (новый, хранение); отправка push → TD-011 | — | **3** |
| **Actions (Plan Week, …) + Choose style** | ⏳ Q-016-1 | дефолт: клиентские пресеты (backend не нужен) | — | — |
| **Web search (toggle)** | ✅ **реализовано** | режим диалога `search` (OpenAI Responses `web_search`), [Q-016-2](99-open-questions.md) Closed [ADR-059](adr/ADR-059-openai-default-provider.md)/[ADR-055](adr/ADR-055-dialog-mode.md) | ADR-055/059 | — |

## Новые модули (8)
| Модуль | Каталог | Спринт | Суть |
|---|---|---|---|
| Chats ✅ | [modules/chats/](modules/chats/README.md) | 1 (реализован) | CRUD/список/поиск/steps-view поверх chat_sessions |
| Profile ✅ | [modules/profile/](modules/profile/README.md) | 1 (реализован) | displayName + производный accountId |
| Preferences ✅ | [modules/preferences/](modules/preferences/README.md) | 1 (реализован) | default_assistant_mode, notif toggle, code defaults |
| Workspaces | [modules/workspaces/](modules/workspaces/README.md) | 2 | рабочие пространства чатов (≠ website-builder) |
| Snippets | [modules/snippets/](modules/snippets/README.md) | 2 | сохранённые код-фрагменты |
| Attachments | [modules/attachments/](modules/attachments/README.md) | 3 (отложен, [TD-015](100-known-tech-debt.md)) | двухшаговый upload-модуль (таблица `attachments`). **Мультимодальный ввод (vision/document) уже реализован на MVP inline base64 в `/chat/run` ([ADR-020](adr/ADR-020-inline-base64-attachments-mvp.md), строка 51) — без этого модуля.** |
| Token Purchase | [modules/token-purchase/](modules/token-purchase/README.md) | 3 | consumable IAP → grant кредитов |
| Notifications | [modules/notifications/](modules/notifications/README.md) | 3 | toggle + device push-token (push-отправка → TD-011) |

## Новые ADR (5)
- [ADR-012](adr/ADR-012-assistant-mode-vs-billing-mode.md) — `assistant_mode` (chat/code) vs `billing_mode` (credits/byok). Критично: разводит терминологию «mode».
- [ADR-013](adr/ADR-013-workspace-projects-vs-website-builder.md) — workspace-проекты как отдельный модуль, не website-builder `projects`.
- [ADR-014](adr/ADR-014-multimodal-attachments.md) — двухшаговые вложения (upload → ссылка в /chat/run). **Транспорт Superseded → [ADR-020](adr/ADR-020-inline-base64-attachments-mvp.md)** (inline base64 в /chat/run для MVP); двухшаговая модель отложена ([TD-015](100-known-tech-debt.md)).
- [ADR-020](adr/ADR-020-inline-base64-attachments-mvp.md) — мультимодальный ввод inline base64 в /chat/run (MVP), без отдельного модуля/таблицы.
- [ADR-015](adr/ADR-015-consumable-token-iap.md) — consumable IAP → идемпотентный grant кредитов.
- [ADR-016](adr/ADR-016-extended-byok-statuses.md) — расширенные BYOK-статусы + активная модель.

## Изменения схемы (expand-only)

**Применено миграцией `0004` (MVP):**
- `users.display_name` (profile).
- `chat_sessions`: `title`, `assistant_mode`, `is_pinned` (chats). **`workspace_project_id` — НЕ в `0004`** (Спринт 2, отдельная будущая миграция).
- Таблица `user_preferences`.
- Enum `assistant_mode`; расширение enum `byok_key_status` (`validating`/`offline`/`expired`).

**Спроектировано, на MVP миграцией НЕ создаётся:**
- `workspace_projects` + `chat_sessions.workspace_project_id` — предпосылка Спринта 2, отдельная будущая миграция (НЕ `0004`).
- `snippets` (Спринт 2) и `device_push_tokens` (notifications, Спринт 3) — отдельными будущими миграциями (НЕ `0004`).
- **Отложены ([TD-015](100-known-tech-debt.md)):** `workspace_files`, `attachments` (двухшаговый transport [ADR-014](adr/ADR-014-multimodal-attachments.md) Superseded; chat-вложения MVP — inline base64 [ADR-020](adr/ADR-020-inline-base64-attachments-mvp.md)). Enum `attachment_kind` объявлен в сводном DDL, но миграцией на MVP не применяется.
- Полный DDL и статусы — [03-data-model.md](03-data-model.md).

## Приоритизация по спринтам и зависимости

### Спринт 1 — ядро приложения (✅ РЕАЛИЗОВАН, offline-сьют 681/681, production-ready)
Модули: **chats**, **profile**, **preferences**, расширение **byok** (ADR-016), поле **assistantMode** в `/chat/run` (ADR-012).
- Реализовано: `GET /v1/chats` (список/поиск `q`/курсорная пагинация), `GET /v1/chats/{id}` (история), `GET /v1/chats/{id}/steps` (steps-view), `PATCH /v1/chats/{id}` (rename/pin), `DELETE /v1/chats/{id}`; `GET`/`PATCH /v1/profile`; `GET`/`PATCH /v1/preferences`; BYOK `keyStatus` 6 значений + `activeModel`; `assistantMode` (chat\|code) в `/chat/run` с fallback preferences→`chat`.
- Зависимость закрыта: миграция `0004` применена (поля chat_sessions `title`/`assistant_mode`/`is_pinned` + индекс `ix_sessions_user_pinned_updated`, users.display_name, таблица user_preferences, enum assistant_mode, расширение byok_key_status). Цепочка `0001`→`0002`→`0003`→`0004`.
- assistant_mode фиксируется на сессию в orchestrator; fallback из preferences (`defaultAssistantMode`).
- `chat_sessions.workspace_project_id` НЕ создан в `0004`; создаётся в миграции `0011` (Поставка 3, [ADR-036](adr/ADR-036-workspaces-implementation.md)). До `0011` в ответе `GET /v1/chats` поле `workspaceProjectId` = `null` (заглушка).

### Спринт 2 / Поставка 3 — рабочие пространства и Code-режим (⏳ спроектирован, ожидает реализации)
Модули: **workspaces** (Поставка 3, [ADR-036](adr/ADR-036-workspaces-implementation.md)), **snippets**.
- **workspaces:** миграция `0011` создаёт `workspace_projects` + `chat_sessions.workspace_project_id` + **`workspace_files` (BYTEA, собственное хранение)**. Файлы-знания **самодостаточны** — **сняли зависимость от отложенного `attachments`** ([ADR-036 §4](adr/ADR-036-workspaces-implementation.md), разблокирует [TD-015](100-known-tech-debt.md)-зависимость). Под-фаза 3A (ядро: CRUD + instructions + привязка) и 3B (файлы-знания) реализуемы вместе.
- snippets — независим (только своя таблица, отдельная будущая миграция).

### Спринт 3 — ввод и интеграции (⏳ спроектирован, ожидает реализации; частично зависит от ответов пользователя)
Модули: **attachments** (двухшаговый upload, отложен), **token-purchase**, **notifications**.
- **Мультимодальный ввод (chat-вложения) — реализован на MVP inline base64 ([ADR-020](adr/ADR-020-inline-base64-attachments-mvp.md)), не входит в этот спринт** (см. строку 51). PDF отдаётся Claude нативным `document`-блоком; `pypdf` — только page-guard (анти-bomb), НЕ extractor; multipart НЕ используется.
- attachments (двухшаговый upload-модуль, **отложен** [TD-015](100-known-tech-debt.md)): таблица `attachments`, `POST /v1/attachments` для персистируемых файлов-контекста (workspace), резолв ссылок в `/chat/run`. Разблокирует полноценные workspace-файлы (Спринт 2 Phase 3–4). Транспорт прежней редакции — [ADR-014](adr/ADR-014-multimodal-attachments.md) (Superseded).
- token-purchase: reuse StoreKit verifier + Wallet.grant; [Q-015-1](99-open-questions.md) Closed = вариант B — покупка требует активной подписки (policy-guard перед grant, `403 subscription_required`).
- notifications: хранение токена/настройки; отправка push → [TD-011](100-known-tech-debt.md) (отдельный поздний проход).

### Steps-view (дизайн «3 steps»)
Реализован в **chats** (`GET /v1/chats/{id}/steps`) — данные из `chat_steps`/`tool_calls`, Спринт 1.

## Открытые вопросы для пользователя (ждут ответа пользователя)
Все три ниже относятся к Спринтам 2/3 и **не блокируют** реализованный Спринт 1. Статус каждого — **ожидает ответа пользователя**:
- **[Q-015-1](99-open-questions.md) — Closed (2026-06-02, вариант B):** покупка токенов **требует активной подписки** (докупка сверх месячного пакета). Без активной подписки `POST /v1/tokens/purchase` → `403 subscription_required` (policy-guard до grant). [ADR-002](adr/ADR-002-access-policy-state-machine.md) без изменений; «мёртвый» баланс устранён. Backend-доработка — [modules/token-purchase/07-implementation-phases.md Phase 4](modules/token-purchase/07-implementation-phases.md).
- **[Q-016-1](99-open-questions.md)** — Actions (Plan Week, Meeting Notes, …) и Choose style (Normal/Learning/Concise/Formal): серверные пресеты или клиентские? Дефолт: **клиентские** (backend-эндпоинт не нужен; стиль через `context.style`). **Ждёт ответа пользователя.**
- **[Q-016-2](99-open-questions.md)** — Web search toggle: backend-tool или клиент? **Блокер фичи** — не реализуем до выбора провайдера поиска. **Ждёт ответа пользователя.**
- Неблокирующие: [Q-012-1](99-open-questions.md) (tool-реестр по assistant_mode), [Q-013-1](99-open-questions.md) (инъекция workspace-контекста), [Q-014-1](99-open-questions.md)/[Q-014-2](99-open-questions.md) (allowlist/лимиты вложений).

## Сохранённые инварианты
- Биллинг ([ADR-006](adr/ADR-006-credit-billing-and-subscription-grant.md)): «1 кредит = 1 сообщение» — без изменений; добавлен второй источник credit-tx (consumable purchase, ADR-015), идемпотентность ledger ([ADR-005](adr/ADR-005-idempotency-ledger.md)) сохранена.
- Policy ([ADR-002](adr/ADR-002-access-policy-state-machine.md)): `billing_mode`/policy не затронуты; `assistant_mode` ортогонален.
- Provisioning ([ADR-007](adr/ADR-007-lazy-user-provisioning.md)): новые таблицы FK на `users`, lazy-provisioning покрывает.
- Tool-loop ([ADR-008](adr/ADR-008-provider-tool-use-id.md)/[ADR-011](adr/ADR-011-server-side-tools.md)): steps-view отдаёт только доменные имена; server-side/client-side tools не меняются.
- BUG-1/3/4 фиксы, terminology существующих модулей — не затронуты. Website-builder `projects` ≠ workspace-проекты ([ADR-013](adr/ADR-013-workspace-projects-vs-website-builder.md)).
