# Chat Orchestrator — Events

На старте система без брокера сообщений (см. [ADR-001](../../adr/ADR-001-stack-choice.md)) — «события» реализуются как синхронные вызовы Audit Service и записи в `audit_logs`, а не как сообщения шины.

## Аудируемые события (через Audit Service)
| event_type | Когда | payload (без секретов) |
|---|---|---|
| `policy_decision` | после каждого вызова Policy в /chat/run | `{mode, decision, blockReason?}` |
| `chat_step` | после записи шага | `{sessionId, role, model, usage}` |
| `tool_call_initiated` | при создании tool_calls | `{toolCallId, toolName}` |
| `tool_call_completed` | при tool-result (мутирующие tools) | `{toolCallId, toolName, status}` |
| `billing_debit` | при списании mode=credits | делегируется Wallet → audit `billing_debit` |

## Tool lifecycle log (наблюдаемость)
`tool_call_initiated → tool_call_completed/errored` логируется с correlation id и метрикой `tool_call_roundtrip_latency_seconds`.

> Внедрение брокера (если понадобится async-обработка) — потребует отдельного ADR.
