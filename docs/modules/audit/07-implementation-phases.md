# Audit — Implementation Phases

| Phase | Задача | Зависит от |
|---|---|---|
| AU-1 | Модель + миграция audit_logs, индексы. | DB |
| AU-2 | `record(event)` + redactor (denylist секретов). | AU-1 |
| AU-3 | Интеграция вызовов: Orchestrator (tool_mutation, policy, chat_step), Wallet (billing), BYOK, Subscription. | AU-2 + соответствующие модули |

> AU-1/AU-2 реализуются рано (нужны всем модулям для AC-7).
