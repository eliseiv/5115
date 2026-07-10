# BYOK — Overview

## Scope
- `POST /v1/byok/set` — сохранить пользовательский ключ **любого поддерживаемого провайдера** (Anthropic/OpenAI, [ADR-044](../../adr/ADR-044-multi-provider-byok.md)) зашифрованно (envelope encryption), детектировать провайдера по ключу, валидировать через провайдера ключа.
- `POST /v1/byok/toggle` — вкл/выкл использование ключа.
- `POST /v1/byok/delete` — удалить ключ (и зашифрованные материалы).
- Внутренний `get_plaintext_key(userId)` — расшифровка и выдача Orchestrator in-memory на время вызова.

## Out of scope
- Решение, разрешён ли byok (Policy Engine: требует активной подписки + enabled + valid).
- Сам вызов LLM-провайдера в режиме byok (Orchestrator; провайдер определяется по ключу — [ADR-044](../../adr/ADR-044-multi-provider-byok.md)).

## Безопасность ([ADR-003](../../adr/ADR-003-byok-envelope-encryption.md), [05-security.md](../../05-security.md))
- Plaintext ключ никогда не хранится и не логируется.
- AES-256-GCM с DEK; DEK зашифрован KMS.
- Ключ не возвращается клиенту никогда (только статус).
