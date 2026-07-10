# ADR-059 — OpenAI как провайдер по умолчанию через Responses API; Deep Thinking форсит reasoning-модель

- **Статус:** Accepted
- **Дата:** 2026-07-09
- **Связано:** [ADR-033](ADR-033-llm-provider-abstraction.md) (провайдер-абстракция `LLMClient`, нормализованный `stop_reason`, один провайдер на инстанс — фундамент), [ADR-041](ADR-041-openai-native-pdf-attachment.md) (wire-формат вложений OpenAI — пересматривается), [ADR-044](ADR-044-multi-provider-byok.md) (мульти-провайдерный BYOK, stale-model фолбэк), [ADR-055](ADR-055-dialog-mode.md) (режимы, опирающиеся на Responses), [ADR-034](ADR-034-user-model-selection.md) (session-fixed `model`), [ADR-058](ADR-058-image-generation.md) (`images.generate`), [Q-033-2](../99-open-questions.md) (Responses API), [Q-016-2](../99-open-questions.md) (веб-поиск — закрывается)

## Контекст

Новые режимы диалога ([ADR-055](ADR-055-dialog-mode.md)) — Search, Deep Thinking, Study & Learn — и генерация изображений ([ADR-058](ADR-058-image-generation.md)) требуют возможностей, которых текущий `OpenAIClient` на **Chat Completions** ([ADR-033](ADR-033-llm-provider-abstraction.md)) не даёт. `pyproject.toml` разрешал `openai>=1.51`; `uv.lock` зафиксировал `1.109.1` (Responses API и `gpt-image-1` присутствуют). Дефолтный провайдер в коде — `anthropic` ([ADR-033](ADR-033-llm-provider-abstraction.md)); `.env` прод-инстансов `LLM_PROVIDER` не задаёт → работает Anthropic.

## Решение

### 1. Responses API вместо Chat Completions (переписать `OpenAIClient`)

`chat.completions.create` → `client.responses.create`. Причина — **веб-поиск у OpenAI доступен только в Responses API**: в Chat Completions поиск возможен лишь через preview-модели, не комбинируемые с нашими function-tools. Режим Search без Responses не реализуем — это единственный путь, не рефактор ради красоты. Заодно Responses несёт reasoning (Deep Thinking) и единый tool-контракт.

Ключевые отличия wire-формата (подтверждены интроспекцией pinned SDK 1.109.1, не выдуманы):
- **`FunctionToolParam` плоский:** `{type: 'function', name, parameters, strict, description}` — `name` наверху, БЕЗ вложенного `function` (отличие от Chat Completions). Обновляется `openai_tool_function` в `tools.py` (SSOT).
- `max_tokens` → `max_output_tokens`.
- `input` — список items: `{type: 'message', role, content: [{type: 'input_text', ...}]}`; tool-результат → `{type: 'function_call_output', call_id, output}`.
- Разбор `output[]` по типам item: `message` (внутри `output_text` с `annotations`), `reasoning`, `function_call`, `web_search_call`.
- `function_call` несёт **`call_id`** (`call_...`) — именно он пишется в `provider_tool_use_id` ([ADR-008](ADR-008-provider-tool-use-id.md)), **не** `id` (`fc_...`).
- Нормализованный `stop_reason` ([ADR-033](ADR-033-llm-provider-abstraction.md)): наличие `function_call` → `tool_use`; `status=="incomplete"` и `incomplete_details.reason=="max_output_tokens"` → `max_tokens`; иначе → `end_turn`. **`web_search_call` НЕ порождает `tool_use`** — иначе провайдерский поиск утёк бы в клиентские `toolCalls[]`.
- `usage`: `input_tokens`/`output_tokens`, `cache_read` из `input_tokens_details.cached_tokens`, `cache_write = 0`.

### 2. Веб-поиск: `web_search` (GA), закрытие Q-016-2

Веб-поиск-tool — `WebSearchToolParam` с `type='web_search'` (GA-имя; legacy `WebSearchPreviewToolParam` **не** используем). Доп. поля: `search_context_size ∈ {low, medium, high}` (`OPENAI_SEARCH_CONTEXT_SIZE`), `filters`, `user_location`. Добавляется рядом с нашими function-tools при `dialog_mode=search`. Цитаты — в `message.content[].output_text.annotations` (тип `url_citation`). Это закрывает **[Q-016-2](../99-open-questions.md)** (провайдер веб-поиска выбран — OpenAI Responses `web_search`).

### 3. Reasoning (Deep Thinking) и реплей encrypted_content

`Reasoning`-param: `effort ∈ {low, medium, high}` (`generate_summary` — deprecated alias, используем `effort`). Reasoning-items **обязаны реплеиться** в tool-loop, иначе OpenAI отвергнет continuation: `store=False` + `include=["reasoning.encrypted_content"]` (имя флага подтверждено интроспекцией SDK), items кладутся в `content_blocks`.

### 4. Deep Thinking форсит `DEEP_THINKING_MODEL` — включая BYOK

При `dialog_mode=deep_thinking` ([ADR-055](ADR-055-dialog-mode.md)) `effective_model = DEEP_THINKING_MODEL`, **переопределяя** и выбор пользователя `sess.model` ([ADR-034](ADR-034-user-model-selection.md)), и stale-model guard ([ADR-044](ADR-044-multi-provider-byok.md)). Reasoning требует профильной модели; обычная выбранная пользователем модель может не поддерживать `reasoning.effort`.

**Следствие для BYOK — фиксируется явно как принятое:** для `mode=byok` Deep Thinking возьмёт `DEEP_THINKING_MODEL` инстанса, то есть **ключ пользователя потратится на модель инстанса**, а не на выбранную пользователем. Это осознанный компромисс: Deep Thinking — это «инстанс знает, какая reasoning-модель нужна». Пользователь, выбравший BYOK и Deep Thinking, тратит свой ключ на `DEEP_THINKING_MODEL`. Явно зафиксировано, чтобы не считать это багом.

### 5. Смена дефолта — через env инстансов, НЕ в коде

Дефолтный провайдер меняется на OpenAI **через env инстанса** (`LLM_PROVIDER=openai`), а **не** сменой кодового дефолта. Если поменять дефолт в коде, anthropic-инстансы, полагающиеся на дефолт (не задающие `LLM_PROVIDER`), **молча переключатся** на OpenAI и упадут (нет `OPENAI_API_KEY`, чужой wire-формат в БД). Поэтому:
- Кодовый дефолт `LLM_PROVIDER` остаётся `anthropic` ([ADR-033](ADR-033-llm-provider-abstraction.md)).
- Инстансы, которым нужен OpenAI и новые режимы, **явно** ставят `LLM_PROVIDER=openai` + `OPENAI_API_KEY`.
- **Существующие anthropic-инстансы, полагавшиеся на дефолт, ОБЯЗАНЫ явно задать `LLM_PROVIDER=anthropic`** — проверяемое допущение фиксируется в `.env.example`/`07-deployment.md`. Это защита на случай, если будущее решение сменит кодовый дефолт.

Anthropic остаётся полностью рабочим для BYOK ([ADR-044](ADR-044-multi-provider-byok.md)) и обратной совместимости, но **без новых режимов** (`deep_thinking|study_learn|search` provider-gated на OpenAI, [ADR-055 §4](ADR-055-dialog-mode.md)); паритет — [TD-030](../100-known-tech-debt.md).

### 6. Смена wire-формата вложений (пересматривает ADR-041)

Responses ждёт `input_image`/`input_file` вместо `image_url`/`file` из Chat Completions. Значит маппинг вложений ([ADR-041](ADR-041-openai-native-pdf-attachment.md), нативный PDF на OpenAI) входит в объём: openai-ветка `attachments.py` переводится на Responses-формат. Anthropic-ветка не трогается. Это пересмотр транспорта ADR-041 в части OpenAI (тело ADR-041 не переписывается — immutability; актуальный wire — здесь).

### 7. Констрейнт зависимости

`pyproject.toml`: `openai>=1.99,<2` (текущий lock `1.109.1`). `<2` — мажор может ломать (не `<3`). `anthropic` **не трогаем** (`>=0.39,<0.40`).

### 8. Проверяемое допущение о старых OpenAI-сессиях

Wire-формат персиста для OpenAI меняется (Chat Completions → Responses), поэтому старые OpenAI-сессии стали бы нечитаемы при реплее. Прод работает на `LLM_PROVIDER=anthropic` (дефолт, `.env` не задаёт), поэтому OpenAI-сессий со старым Chat Completions payload быть не должно. Это **проверяемое допущение** — подтвердить до мержа (если такие сессии есть — нужен адаптер чтения старого payload).

## Альтернативы

- **Остаться на Chat Completions.** Отклонено: веб-поиск там только через preview-модели, не комбинируемые с function-tools → режим Search нереализуем; reasoning-replay и единый tool-контракт удобнее в Responses.
- **Сменить кодовый дефолт `LLM_PROVIDER` на `openai`.** Отклонено: anthropic-инстансы, полагающиеся на дефолт, молча переключились бы и упали. Смена — только через env инстансов.
- **Реализовать режимы на Anthropic тоже.** Отклонено на эту поставку ([TD-030](../100-known-tech-debt.md)): Anthropic-путь не несёт Responses-механизмов; переписывать оба клиента — вне scope.
- **Не форсить модель для Deep Thinking (уважать `sess.model`/BYOK).** Отклонено: выбранная модель может не поддерживать `reasoning.effort`; инстанс знает нужную reasoning-модель. BYOK-следствие принято явно (§4).
- **`openai>=1.99,<3`.** Отклонено: мажор 2.x может ломать tool-loop/usage-parsing; `<2` безопаснее.
- **Оставить legacy `web_search_preview`.** Отклонено: GA-имя `web_search` — стабильный контракт; preview устаревает.

## Последствия

- **Положительные:** Responses API открывает Search/Deep Thinking/квизы/изображения; веб-поиск-провайдер выбран ([Q-016-2](../99-open-questions.md) закрыт); нормализованный `stop_reason` сохранён ([ADR-033](ADR-033-llm-provider-abstraction.md)); Anthropic остаётся для BYOK и обратной совместимости; смена дефолта безопасна (через env, с явным `LLM_PROVIDER=anthropic` на старых инстансах).
- **Цена:** переписать `OpenAIClient` на Responses (wire-формат tools/input/output/usage/stop_reason); перевести openai-ветку `attachments.py` на `input_image`/`input_file` (ADR-041); поднять `openai>=1.99,<2`; новые config `DEEP_THINKING_*`/`OPENAI_SEARCH_CONTEXT_SIZE`.
- **Tech debt:** новые режимы только на OpenAI ([TD-030](../100-known-tech-debt.md)).
- **Безопасность:** `OPENAI_API_KEY` — секрет, под redaction; веб-поиск-цитаты — публичные URL; reasoning `encrypted_content` реплеится, но не логируется; BYOK-ключ на Deep Thinking тратится на `DEEP_THINKING_MODEL` (зафиксировано §4). **Prompt-injection через результаты `web_search` (режим Search) — ПРИНЯТЫЙ остаточный риск:** внешний веб-контент, попадающий в ход, несёт authority ассистента, не системы; backend не санитизирует смысл страниц (надёжно невозможно), митигация — ограничение поверхности ущерба (мутирующие tools требуют действия пользователя/скоупа владельца, биллинг не обходится), а не предотвращение инъекции. Полная модель и границы — [05-security.md §Веб-поиск](../05-security.md#веб-поиск-режим-search-prompt-injection-через-результаты--принятый-остаточный-риск-adr-059).
