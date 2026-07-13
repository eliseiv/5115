# Audit — API Contracts

Нет публичного HTTP API на старте (admin/просмотр аудита — out of scope bootstrap).

## Внутренний контракт
```
record(event: AuditEvent) -> None
AuditEvent = {
  userId: uuid,
  sessionId: uuid | None,
  eventType: str,          # tool_mutation | billing_debit | billing_credit |
                           # policy_decision | byok_change | subscription_change |
                           # chat_step | tool_call_initiated | tool_call_completed |
                           # admin_grant | admin_subscription_grant
  payload: dict            # без секретов
}
```
- Запись синхронная в рамках той же бизнес-транзакции, где это уместно (например, billing_debit — в транзакции списания), иначе сразу после.
- `payload` проходит redaction-проверку: запрещены ключи `*key*`, `*token*`, `*secret*`, raw StoreKit/BYOK.

## Каталог eventType
| eventType | Источник | Обязателен для AC |
|---|---|---|
| `tool_mutation` | Orchestrator (server-side site.write_file/site.delete; client-side мутирующие `files.*`/`calendar.*`/`reminders.*` удалены — [ADR-063](../../adr/ADR-063-remove-client-side-calendar-reminders-files-tools.md); старые записи сохраняются) | AC-7 |
| `billing_debit` | Wallet | AC-7 |
| `billing_credit` | Wallet | — |
| `policy_decision` | Orchestrator | — |
| `byok_change` | BYOK | — |
| `subscription_change` | Subscription | — |
| `chat_step` | Orchestrator | — |
| `tool_call_initiated` / `tool_call_completed` | Orchestrator | — |
| `admin_grant` | Admin (начисление кредитов оператором; actor=admin, reason, без секрета) | — |
| `admin_subscription_grant` | Admin (ручная активация/продление подписки; actor=admin, plan/status/expiresAt/creditsGranted, без секрета; [ADR-048](../../adr/ADR-048-admin-subscription-grant.md)) | — |
