# 06 — Testing Strategy

## Пирамида
| Уровень | Доля | Что покрывает | Инструменты |
|---|---|---|---|
| Unit | ~60% | Чистая логика: Policy Engine (state machine), биллинг-правило (1 кредит = 1 сообщение / 1 списание на message-шаг, ADR-006), валидация tool-схем, encryption helpers. | pytest, без I/O |
| Integration | ~30% | Endpoint + реальные PostgreSQL/Redis (testcontainers), миграции, идемпотентность, атомарность ledger. Внешние HTTP (Anthropic/Apple) — мок через respx. | pytest-asyncio, testcontainers, respx |
| E2E | ~10% | Полные сценарии: trial-once, blocked при истёкшей подписке, tool-loop в несколько шагов, BYOK routing. | pytest против поднятого app + контейнеры |

## Coverage gate
- Глобальный минимум: **80%** (`--cov-fail-under=80`, см. [02-tech-stack.md](02-tech-stack.md)).
- Критические пакеты (`policy`, `wallet`, `byok`) — целевое покрытие **≥ 95%**, проверяется per-package в CI.

## Обязательные тест-кейсы (привязка к AC из 00-vision)
| Тест | AC | Уровень |
|---|---|---|
| Trial доступен ровно 1 раз, второй → `trial_used` | AC-1 | integration |
| `/chat/run` blocked при `subscription=expired`, mode=credits и mode=byok | AC-2 | integration |
| Конкурентные `consume` с одним idempotency key (для chat-debit — один `messageStepId`) → одно списание | AC-3 | integration |
| Re-entry message-шага (`/chat/run` + N×`/chat/tool-result`) → ровно один debit по `messageStepId` | AC-3, AC-4 | e2e |
| `consume` при balance < amount → отказ, баланс не отрицателен | AC-3 | unit+integration |
| Tool-loop: run → tool_call → tool-result → tool_call → ... → assistant_message | AC-4 | e2e |
| Повторный tool-result с тем же `toolCallId` → идемпотентно | AC-4 | integration |
| BYOK ключ зашифрован в БД; логи не содержат plaintext | AC-5 | integration |
| `/policy/effective` совпадает с фактическим решением `/chat/run` для всех состояний | AC-6 | integration |
| Audit-запись на каждое мутирующее tool-действие и каждое списание | AC-7 | integration |

## Тест-кейсы мультимодальных вложений (ADR-020)
| Тест | Уровень |
|---|---|
| `image` (jpeg/png/gif/webp) → корректный Anthropic `image`-блок (base64, media_type из записи) | unit |
| `document` (PDF) на Anthropic → нативный `document`-блок base64; текст НЕ извлекается | unit |
| `document` (PDF) на OpenAI (`LLM_PROVIDER=openai`) → **НЕ `422`** ([ADR-041](adr/ADR-041-openai-native-pdf-attachment.md), закрывает [TD-023](100-known-tech-debt.md)): content-часть `file` (data-URI) ИЛИ извлечённый `pypdf`-текст как text-блок (фолбэк); turn-0 сборка/плейсхолдеры/персист без изменений (сырой base64 не персистится) | unit |
| `text` (plain/markdown/csv/json) → `text`-блок с разметкой имени файла; невалидный UTF-8 → `422` | unit |
| MIME вне allowlist (DOCX/HEIC/zip/octet-stream) → `422 unsupported_media_type` | unit+integration |
| Рассогласование `type`/`mediaType` ↔ magic bytes (бинарь под видом image/png) → `422` | unit |
| Невалидный/обрезанный base64 → `422` (не 500) | unit |
| Лимит размера одного вложения / суммарного / числа — проверка ДО декодирования → `413`/`422` | unit+integration |
| Повышенный body-лимит применяется **только** к upload-роутам `/v1/chat/run` (ADR-020) и `POST /v1/workspaces/{id}/files` (ADR-045); прочие роуты (включая CRUD `/v1/workspaces/{id}` и `DELETE …/files/{file_id}`) сохраняют `≤512KB` | integration |
| Workspace upload (ADR-045): файл ровно 8 MB (`WORKSPACE_FILE_MAX_BYTES`) в base64 проходит gateway (не 413 на транспорте); тело > `WORKSPACE_REQUEST_BODY_LIMIT` → `413`; инвариант `WORKSPACE_REQUEST_BODY_LIMIT ≥ WORKSPACE_FILE_MAX_BYTES*4/3 + JSON-запас` | unit+integration |
| PDF page-guard: PDF с числом страниц > `ATTACHMENT_PDF_MAX_PAGES` → `422` (анти-bomb) | unit |
| URL-вложение / `source.type=url` → отвергается (нет backend-fetch, анти-SSRF) | unit |
| Реплей: `chat_steps.payload` user-turn содержит плейсхолдер, НЕ base64; на витке ≥1 tool-loop тяжёлый контент не реплеится | integration |
| Биллинг: сообщение с вложениями = 1 кредит (mode=credits и mode=byok); usage пишется в meta | integration |
| Логи/audit не содержат `attachments[].data` и декодированного содержимого (redaction) | integration |
| Вложения в `/chat/run` принимаются; `/chat/tool-result` их не принимает (`extra='forbid'`) | unit |
| **E2E (реальный Anthropic):** image + PDF + text в одном сообщении → корректный assistant_message; подтверждает wire-совместимость `document`-блока на SDK 0.39.0 ([TD-016](100-known-tech-debt.md)). **Статус: обязателен, но пока НЕ выполнен — org Anthropic отключена (generation blocked); прогон обязателен сразу после восстановления org. До прогона live-совместимость PDF `document`-блока остаётся неподтверждённой (TD-016 открыт).** | e2e (`@pytest.mark.external`) |

## Тест-кейсы инструмента `time.now` ([ADR-026](adr/ADR-026-global-server-side-tools-and-time-now.md))

**Контракт Clock для qa (детерминизм).** `time.now` берёт время через инъектируемый `Clock` (Protocol с `now() -> datetime` timezone-aware UTC; дефолт `SystemClock`). Тесты подают `FixedClock(fixed_dt)` в `GlobalToolHandlers` → результат полностью детерминирован; **прямой `datetime.now()` в коде `time.now` запрещён** (иначе тест недетерминирован). qa проверяет точный JSON-шейп при фиксированном `fixed_dt`.

| Тест | Уровень |
|---|---|
| Без `tz`: result = `{utc, unix, weekday}` (ISO8601 `+00:00`, целочисленный unix, верный день недели по `fixed_dt`); полей `local`/`timezone` НЕТ | unit |
| С валидным `tz` (`Europe/Moscow`): дополнительно `local` (ISO8601 с offset зоны) + `timezone` (нормализованное имя); `utc`/`unix`/`weekday` соответствуют `fixed_dt` | unit |
| Невалидный/неизвестный `tz` (`Mars/Phobos`, мусор) → `ToolExecution.error(code="invalid_timezone")`, НЕ исключение/падение хода; ход продолжается | unit |
| `tz` длиннее лимита (`> 64`, [Q-026-1](99-open-questions.md)) → `invalid_timezone` (до резолва `zoneinfo`) | unit |
| Args `extra=forbid`: лишний ключ в args → ошибка валидации (как у прочих tools) | unit |
| Детерминизм: `FixedClock` → одинаковый результат при повторных вызовах; `SystemClock` (дефолт) даёт текущее время | unit |
| Маршрутизация global server-side: `time.now` исполняется в tool-loop **без проекта** (`project_id IS NULL`) — `_external_project_id` НЕ вызывается, `assert external_project_id is not None` не срабатывает; в `toolCalls[]` наружу НЕ попадает; loop продолжается к Anthropic | integration |
| `anthropic_tool_definitions(include_server_side=False)` (нет проекта) содержит `time.now`, НЕ содержит `site.*`; `GET /v1/tools` → 14 tools, `time.now`: `execution=server`, `mutating=false` | unit+integration |
| Биллинг: сообщение с раундом(ами) `time.now` = 1 кредит (mode=credits) — server-side раунд не добавляет списаний | integration |
| Системный промт (chat и code) содержит статичную time.now-инструкцию; промт стабилен между запросами (prompt cache не инвалидируется — дата НЕ в промте) | unit |
| **E2E (реальный Anthropic):** запрос «какое сегодня число / какой день недели» в «чистом чате» без проекта → Claude вызывает `time.now`, отдаёт верную дату (не «2024») | e2e (`@pytest.mark.external`) |

> **tzdata-зависимость ([TD-019](100-known-tech-debt.md) Resolved 2026-06-10).** Тесты локального времени (`tz` → `local`/`timezone`) требуют tz-базы в тестовом окружении. tz-база обеспечена pure-Python зависимостью `tzdata` (`pyproject.toml`/`uv.lock`), входящей и в dev-, и в prod-окружение → валидный `tz` резолвится в тестах и в prod. UTC-кейсы tz-базы не требуют.

## Политика моков
- **PostgreSQL и Redis — реальные** (testcontainers). Не мокать БД.
- **Anthropic API, App Store Server API, KMS** — мокаются (respx / fakes). Реальные вызовы только в отдельном `@pytest.mark.external` наборе (вне CI по умолчанию).

## State-machine тестирование Policy Engine
Полная таблица переходов из [ADR-002](adr/ADR-002-access-policy-state-machine.md) покрывается параметризованными unit-тестами: декартово произведение {subscription: none/active/expired} × {trial_used: T/F} × {credits: 0/>0} × {byok: disabled/invalid/valid} × {mode: credits/byok} → ожидаемый `allow|blockReason`.

## Структура
```
tests/
  unit/         # policy, conversion, schemas, crypto
  integration/  # endpoints + db + redis, respx для внешних
  e2e/          # сквозные сценарии
  conftest.py   # фикстуры: app, db container, redis container, jwt factory
```

## CI gate (см. 07-deployment.md)
PR не проходит, если: `ruff format --check` fail, `ruff check` fail, `mypy` fail, `pytest` fail, coverage < 80%.
