# ADR-012 — Разведение терминологии: assistant_mode (тип ассистента) vs billing_mode (способ оплаты)

- Статус: Accepted
- Дата: 2026-06-02
- Связан с: ADR-002 (policy), ADR-006 (billing), модуль `preferences`, `chat-orchestrator`.

## Context
Дизайн iOS-приложения вводит понятие «mode» как **тип ассистента**: `chat` (обычный диалог) vs `code` (кодовый ассистент). Это пользовательская/сессионная настройка, влияющая на дефолтный system-prompt и UI.

В backend уже существует поле `chat_sessions.mode` (enum `chat_mode = {credits, byok}`) — это **способ оплаты** генерации (списывать кредиты vs использовать ключ пользователя). Оно завязано на policy (ADR-002) и billing (ADR-006) и фигурирует в контракте `/v1/chat/run` (`"mode": "credits | byok"`).

Если ввести дизайн-«mode» под тем же именем — возникнет фатальная путаница: два разных понятия с одинаковым именем `mode` в одном контракте и в одной таблице. Это сломает либо биллинг, либо UI.

## Decision
Развести два независимых поля с явными именами:

1. **`billing_mode`** — способ оплаты. Это существующее `chat_sessions.mode` (enum `chat_mode = {credits, byok}`). **Сам enum и колонка БД НЕ переименовываются** (избегаем breaking-миграции и ломки инвариантов ADR-002/006). В контракте `/v1/chat/run` поле остаётся `"mode": "credits | byok"` — **публичный контракт не меняется** (обратная совместимость). В документации и новых полях термин — `billing_mode`.

2. **`assistant_mode`** — тип ассистента: enum `assistant_mode = {chat, code}`. Новое понятие. Хранится:
   - как пользовательский дефолт — `user_preferences.default_assistant_mode` (модуль `preferences`);
   - как атрибут сессии — `chat_sessions.assistant_mode` (новая колонка, NOT NULL DEFAULT 'chat'), фиксируется при создании сессии.
   - В контракте `/v1/chat/run` — новое **опциональное** поле `assistantMode: "chat" | "code"` (при отсутствии — берётся `user_preferences.default_assistant_mode`, при отсутствии preferences — `chat`).

### Семантика assistant_mode на backend
- `assistant_mode` влияет на **выбор базового system-prompt** оркестратором (chat-режим — обычный ассистент; code-режим — кодовый ассистент с уклоном в технические ответы) и на **состав tool-реестра**, доступного Claude (например, `site.*` и `files.*` уместны в `code`).
- `assistant_mode` **НЕ влияет** на policy/billing — это ортогональные оси. Любая комбинация (`assistant_mode` × `billing_mode`) допустима.
- Точный текст base-system-prompt для каждого `assistant_mode` — конфигурируемый шаблон (не хардкод в нескольких местах), единый источник в orchestrator. Конкретные tool-наборы по режиму — [Q-012-1](../99-open-questions.md).
- **Примечание (cross-ref [ADR-022](ADR-022-optional-project-and-tool-gating.md)):** доступность `site.*` дополнительно гейтится наличием `project_id` (ADR-022) **поверх** фильтра по `assistant_mode`. Фильтр по `assistant_mode` (эта ось) — НЕ единственный фильтр `site.*`: итоговое предложение `site.*` Claude — И-композиция `(assistant_mode допускает site.*) AND (project_id IS NOT NULL)`. См. ADR-022 §2.

## Consequences
- Никакой миграции существующего `chat_mode`/`mode` — биллинг и policy не затрагиваются, инварианты ADR-002/006 сохранены.
- Добавляется enum `assistant_mode` и колонка `chat_sessions.assistant_mode` (expand-only миграция).
- Документация обязана использовать `billing_mode` (= existing `mode`) и `assistant_mode` явно, без голого «mode». Существующие документы, где «mode» означает оплату, остаются валидны (это `billing_mode`), но при правках уточняются.
- iOS-контракт `/v1/chat/run` дополняется опциональным `assistantMode`; поле `mode` (billing) без изменений.

## Alternatives
- **Переименовать `chat_mode` → `billing_mode` в БД и контракте.** Отвергнуто: breaking-change публичного контракта `/chat/run`, миграция enum, риск для ADR-002/006. Выгода (косметика) не оправдывает риск.
- **Хранить assistant_mode только на клиенте.** Отвергнуто частично: дефолт и история чатов должны быть консистентны между устройствами; дефолт хранится в preferences (server). Но конкретный per-message override клиент может передавать — это поддержано опциональным полем.
