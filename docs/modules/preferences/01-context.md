# Preferences — Context

## Зависимости
- **API Gateway** — auth, provisioning, роуты `/v1/preferences`.
- **user_preferences** таблица.

## Потребители
- **chat-orchestrator** — читает `default_assistant_mode` как fallback `assistantMode` при отсутствии явного поля в `/chat/run` ([ADR-012](../../adr/ADR-012-assistant-mode-vs-billing-mode.md)).
- **notifications** — читает/уважает `notifications_enabled` (не шлёт push, если выключено, [TD-011](../../100-known-tech-debt.md)).

## Границы
- Preferences не влияет на billing/policy. `assistant_mode` ортогонален `billing_mode` ([ADR-012](../../adr/ADR-012-assistant-mode-vs-billing-mode.md)).
