# Preferences — Overview

## Назначение
Хранение пользовательских настроек: дефолтный тип ассистента (chat|code), флаг уведомлений, дефолты Code-context.

## Scope
- `GET /v1/preferences` — текущие настройки (или дефолты, если строки нет).
- `PATCH /v1/preferences` — частичное обновление (`defaultAssistantMode`, `notificationsEnabled`, `codeDefaults`).

## Out of scope
- Сама отправка push (модуль notifications, [TD-011](../../100-known-tech-debt.md)).
- Тема/локаль UI — клиентские настройки (если не потребуется серверная синхронизация).

## Бизнес-правила
- BR-PF-1: `defaultAssistantMode` ∈ {chat, code}; дефолт `chat`. Используется orchestrator как fallback `assistantMode` ([ADR-012](../../adr/ADR-012-assistant-mode-vs-billing-mode.md)).
- BR-PF-2: `notificationsEnabled` (bool, дефолт `false`) — единый источник настройки уведомлений; регистрация устройства — модуль notifications. Дефолт `false` ([ADR-032](../../adr/ADR-032-notifications-enabled-default-false.md)): privacy-by-default; iOS запрашивает системное разрешение на push сначала, затем включает через `PATCH`. Меняется только дефолт для новых/без-строки пользователей — существующие строки сохраняют явный выбор.
- BR-PF-3: `codeDefaults` — JSON-объект дефолтов Code-режима (например `{ "language": "TypeScript" }`); без секретов; валидируется по размеру (≤ 8KB).
- BR-PF-4: строка `user_preferences` создаётся лениво (upsert при первом PATCH); GET без строки → дефолты.
