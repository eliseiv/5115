# BYOK — Implementation Phases

| Phase | Задача | Зависит от |
|---|---|---|
| BY-1 | Модель + миграция byok_keys. | DB |
| BY-2 | `KmsClient` интерфейс + дефолт-реализация (Q-002-1) + AES-GCM helpers. | — |
| BY-3 | `set` (envelope encrypt + валидация ключа; реализовано через активного провайдера, ревизия → BY-7 валидирует через провайдера ключа). | BY-1, BY-2 |
| BY-4 | `toggle` + `delete`. | BY-1 |
| BY-5 | `get_plaintext_key` (internal, decrypt) для Orchestrator. | BY-2, BY-3 |
| BY-6 | audit byok_change + redaction проверки. | BY-3, Audit |
| BY-7 | **Мульти-провайдерный BYOK ([ADR-044](../../adr/ADR-044-multi-provider-byok.md)).** Детектор `detect_byok_provider` + фабрика `llm_client_for(provider)` + миграция `0013` (`byok_keys.provider`) + ревизия `set_key`/`_active_model_for`/orchestrator byok-пути + stale-model фолбэк credits. См. ниже. | BY-3, BY-5, ADR-033 |

## BY-7 — мульти-провайдерный BYOK ([ADR-044](../../adr/ADR-044-multi-provider-byok.md)): подробные указания backend

Не писать код вне этого scope. Команды lint/format/typecheck/test — из [02-tech-stack.md](../../02-tech-stack.md). Биллинг/policy/tool-loop НЕ трогать.

1. **Детектор** — новый модуль `src/app/byok/provider_detect.py`:
   - `detect_byok_provider(api_key: str) -> str | None`. После `api_key.strip()`: `startswith("sk-ant-")`→`"anthropic"` (проверять **ПЕРВЫМ**); `startswith("sk-proj-")`→`"openai"`; `startswith("sk-")`→`"openai"`; иначе `None`.
   - Чистая функция: не логировать ключ, не raise. Возвращает только `{"anthropic","openai",None}`.
2. **Фабрика** — `src/app/chat/llm_client.py`: добавить `llm_client_for(provider: str) -> LLMClient`:
   - `"anthropic"` → `get_anthropic_client()` (тот же синглтон, чтобы conftest-патч `_anthropic_singleton` работал); `"openai"` → существующий `_openai_singleton` (вынести его создание в общий хелпер, чтобы `get_llm_client()` и `llm_client_for` использовали один синглтон); иначе → `ValueError`.
   - `get_llm_client()` рефакторить на делегирование `llm_client_for(active_provider)` — **сигнатуру и поведение не менять** (по-прежнему читает `LLM_PROVIDER`).
3. **Миграция `0013`** — `migrations/versions/..._0013_byok_keys_provider.py`: `op.add_column("byok_keys", sa.Column("provider", sa.Text(), nullable=True))`; `down_revision="0012"`, single head; downgrade `drop_column`. Без backfill.
4. **Модель** — `src/app/models/tables.py` `BYOKKey`: добавить `provider: Mapped[str | None] = mapped_column(Text, nullable=True)`.
5. **`BYOKService.set_key`** (`src/app/byok/service.py`):
   - В начале `provider = detect_byok_provider(api_key)`.
   - `provider is None` → НЕ вызывать валидацию; `key_status="invalid"`, сохранить зашифрованно, `provider=None`, `enabled=False`; вернуть `BYOKResult(False,"invalid",None)`.
   - Иначе `client = llm_client_for(provider)`; `validation = await client.validate_key(api_key)` (заменить `self._anthropic.validate_key`). Сохранить `provider` в строку (`existing.provider`/новый `BYOKKey(provider=...)`).
   - `active_model=_active_model_for(key_status, provider)`.
6. **`_active_model_for(key_status, provider)`** — расширить сигнатуру `provider` (вместо чтения `settings.llm_provider`): `valid`+`openai`→`openai_byok_default_model`; `valid`+`anthropic`→`byok_default_model`; `valid`+`None`→fallback дефолт активного инстанса (легаси, [TD-029](../../100-known-tech-debt.md)); не `valid`→`None`. Обновить вызовы в `toggle`/`get_status` (передавать `row.provider`).
   - `BYOKResult` дополнить полем `provider` НЕ требуется для публичного контракта; провайдер нужен сервису для генерации — передавать через отдельный internal-аксессор (см. п.7), а не через публичный DTO ответа.
7. **Генерация byok** — `src/app/chat/orchestrator.py`:
   - В `_resolve_api_key` (или рядом) для `mode=byok` получить и провайдера: добавить internal-метод BYOK-сервиса, возвращающий `(plaintext_key, provider)` (provider из `byok_keys.provider`, fallback `detect_byok_provider(plaintext)` при `None`). Не расшифровывать дважды.
   - Выбрать клиент `llm_client_for(byok_provider)` для byok-генерации (вместо `self._deps.llm`). Передавать его в `_generate_loop` (параметризовать клиент по режиму: credits→`self._deps.llm`, byok→`llm_client_for(provider)`).
   - Модель byok: `sess.model` только если в allowlist провайдера ключа, иначе `None`. Нужен provider-aware allowlist целевого провайдера — добавить helper в `config.py` (например `allowed_models_for(provider)`), по образцу `allowed_models()`, читающий `*_models_raw`/`*_model` нужного провайдера. (`provider is None` после fallback → defensive-block `byok_invalid`.)
8. **Stale-model фолбэк (credits)** — `orchestrator.py`: где `model=sess.model or None` идёт в `_generate_loop` (в `run` и `tool_result`), заменить на «`sess.model` если `sess.model in get_settings().allowed_models()` (активный провайдер), иначе `None`». БД не переписывать.
9. **Безопасность:** ключ не логировать (детектор/валидация/генерация); проверить redaction покрывает `apiKey`; не персистить plaintext.

> Интерфейс KMS стабилен; конкретный провайдер (Q-002-1) нужен до prod, не до начала кода.
