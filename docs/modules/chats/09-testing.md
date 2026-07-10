# Chats — Testing

Стратегия — [06-testing-strategy.md](../../06-testing-strategy.md).

## Unit
- Автоген `title` из первого сообщения (усечение, нормализация whitespace, пустое сообщение).
- Сортировка списка: pinned сверху, затем по `updated_at`.
- Пагинация cursor (стабильность при равных `updated_at` — tie-break по `id`).

## Integration
- `GET /v1/chats` — пагинация, поиск `q` (по title и по тексту первого сообщения). Фильтр `workspaceProjectId` — **СПРИНТ 2 (отложено)**, в Спринте 1 эндпоинт его не принимает (см. [02-api-contracts.md](02-api-contracts.md)).
- `GET /v1/chats/{id}` — порядок шагов; чужой чат → `404`.
- **Доменная нормализация `payload` ([ADR-024](../../adr/ADR-024-history-payload-domain-normalization.md)):** в `steps[].payload.content[]` `tool_use.name` — dot (== `/v1/tools` `name`); `tool_use.id` и `tool_result.tool_use_id` — domain `tool_calls.id` (== `/chat/run` `toolCall.id`), **не** provider `toolu_...`; текстовые блоки целы; шаг с `[text, tool_use]` отдаётся полностью (оба блока); provider `toolu_...` отсутствует в ответе; `chat_steps.payload` в БД после отдачи не изменён; карта provider→domain строится одним запросом на сессию (без N+1). Полное нормативное покрытие — [chat-orchestrator/09-testing.md §История: доменная нормализация payload](../chat-orchestrator/09-testing.md#integration--история-доменная-нормализация-payload-adr-024).
- `GET /v1/chats/{id}/steps` — корректный `stepCount`, отсутствие raw provider tool_use.id в ответе.
- `PATCH` rename/pin; `extra='forbid'`; `title` > 200 → `422`.
- `DELETE` — cascade `chat_steps`/`tool_calls`; `attachments.session_id` → NULL; повторный DELETE → `404`.

## Изоляция
- Запрос чужого чата (другой `sub`) на всех эндпоинтах → `404`.
