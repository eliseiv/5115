# Website Builder — Testing

## Unit
- Signed URL: build→verify round-trip ок; подмена `projectId`/`ownerUserId`/`exp` → verify `403`; истёкший `exp` → `403`;
  верификация constant-time (по контракту).
- Path normalization: `..`/абсолютные/`\`/NUL → отклонены; валидный относительный путь → принят.
- Content-type allowlist: тип вне allowlist → отклонён.
- Pydantic-схемы `site.*`: `extra='forbid'`; неверный `encoding`/`contentType` → ошибка валидации.
- domain↔anthropic mapping: `site.write_file ↔ site_write_file` (оба направления); неизвестное имя → ошибка обработки.

## Integration (реальный PostgreSQL)
- `site.write_file`: создаёт `projects` (upsert по `(user_id, external_project_id)`) + `site_files` (upsert по `(project_id, path)`);
  перезапись того же `path` обновляет content/size; `size` консистентен с `length(content)`.
- Лимиты: файл > `PREVIEW_MAX_FILE_BYTES` → `is_error file_too_large`; сумма > `PREVIEW_MAX_PROJECT_BYTES` → `project_too_large`;
  > `PREVIEW_MAX_FILES` → `too_many_files`. Без записи при превышении.
- Изоляция: пользователь A не может прочитать/перезаписать проект пользователя B (tool-контекст из сессии).
- `site.delete` удаляет файл, обновляет `fileCount`/`projectBytes`; audit `tool_mutation`.
- MUTATING tools (`site.write_file`/`site.delete`) пишут audit `tool_mutation`; read/utility — нет.

## Integration — preview endpoint
- Валидный signed URL → `200`, корректный `Content-Type` (из `site_files.content_type`), security-заголовки присутствуют
  (sandbox CSP, `nosniff`, `X-Frame-Options`, `no-store`), нет `Set-Cookie`.
- Невалидная/истёкшая подпись → `403`.
- `ownerUserId` подписи ≠ `projects.user_id` → `403`.
- Чужой/несуществующий `projectId` → `404`; несуществующий `path` → `404`.
- Path-traversal в `{path}` (`../`, абсолютный) → `404` (не выход за проект).

## Integration — tool-loop (server-side)
- `/chat/run` с генерацией сайта: server-side `site.*` исполняются на backend **без** `status=tool_call` клиенту;
  tool-loop продолжается до `assistant_message`; ровно 1 debit (`mode=credits`) на финальном шаге (ADR-006).
- Смешанный шаг (server-side `site.*` + client-side `files.*`): client-side уходит на iOS, server-side исполнены на backend;
  `messageStepId` един.
- Guard: > `MAX_SERVER_TOOL_ROUNDS` server-side раундов → контролируемый отказ + audit, без зацикливания.
- `provider_tool_use_id` для server-side согласован (ADR-008): continuation не падает на Anthropic `400`.

## e2e (дополнить [09-e2e-testing.md](../../09-e2e-testing.md))
- Сценарий «сгенерируй простой сайт → site.write_file (index.html) → site.preview → открыть preview URL → 200 HTML».
- Истёкший/подделанный preview URL → `403`.
