# Chat Orchestrator — Implementation Phases

| Phase | Задача | Зависит от |
|---|---|---|
| CO-1 | Tool-схемы (Pydantic, 8 tools, args/result, `extra=forbid`). | — |
| CO-2 | Anthropic client wrapper (messages API, prompt caching, tools definition, usage parsing). | CO-1, GW config |
| CO-3 | Session/steps repository, реконструкция контекста из chat_steps. | DB schema |
| CO-4 | `/chat/run`: Policy call → generate → status mapping → chat_steps + audit. | CO-2, CO-3, Policy Engine |
| CO-4b | Генерация и персист `messageStepId` (chat_steps/tool_calls); восстановление при re-entry из tool-result. [ADR-005](../../adr/ADR-005-idempotency-ledger.md). | CO-3, CO-4 |
| CO-5 | tool_calls lifecycle + `/chat/tool-result` + идемпотентность (ADR-005). | CO-4, CO-4b |
| CO-6 | mode=byok routing (получение ключа от BYOK Service). | CO-4, BYOK module |
| CO-7 | mode=credits debit (Wallet `consume`, `amount=1` на финальный assistant_message; tool-раунды не списывают; идемпотентность по `messageStepId`, единому на message-шаг и переиспользуемому при re-entry; передаётся в поле `requestId` consume). [ADR-005](../../adr/ADR-005-idempotency-ledger.md), [ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md). | CO-4b, CO-5, Wallet module |
| CO-8 | **Редактирование сообщения** ([ADR-040](../../adr/ADR-040-edit-message-and-regenerate.md)): поле `ChatRunRequest.editMessageStepId: uuid\|None` + валидатор «без `sessionId` → 422»; метод `ChatRepository.truncate_from_message_step(session_id, message_step_id) -> int \| None` (anchor=мин.`seq` user-шага → удалить `chat_steps` `seq>=anchor` **и явно** `tool_calls` усечённых ходов по `message_step_id`; вернуть кол-во удалённых шагов `int`, либо `None` если user-шаг не найден → `404 message_not_found`); 404 — через выделенный `MessageNotFoundError` (`code="message_not_found"`, `errors.py`), не голый `NotFoundError`; вызов в `run()` **до** `add_step(user)` (edit требует resume — иначе `404 message_not_found`); прокидка `edit_message_step_id` из роутера в `orchestrator.run`. **Без миграции.** Биллинг: новый `message_step_id` → новый дебит (CO-7), refund нет. Подробные указания — [ADR-040 §Указания backend](../../adr/ADR-040-edit-message-and-regenerate.md). | CO-4, CO-4b, CO-5, CO-7 |

> Q-004-1 закрыт (ADR-006): CO-7 разблокирован. Правило — 1 кредит = 1 сообщение, 1 списание на пользовательский message-шаг.
> CO-8 ([ADR-040](../../adr/ADR-040-edit-message-and-regenerate.md)): edit+regenerate одним атомарным `/chat/run`; усечение по `seq`, явное удаление `tool_calls` (FK не каскадит при удалении шагов), no-refund.
