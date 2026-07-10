# Preferences — Architecture

## Размещение
Пакет `src/app/preferences/`: репозиторий над `user_preferences` + use-cases (get/patch) + роутер `/v1/preferences`.

## Lazy строки
- GET без строки → возврат дефолтов in-memory (не пишет БД).
- PATCH → `INSERT ... ON CONFLICT (user_id) DO UPDATE` (upsert), обновляются только переданные поля (COALESCE-семантика на уровне use-case).

## Потребление orchestrator
- Orchestrator при `/chat/run` без `assistantMode` читает `default_assistant_mode` (одно чтение; кэш по сессии не требуется — `assistant_mode` фиксируется на сессию при её создании). Отсутствие строки → `chat`.

## Инварианты
- `assistant_mode` (тип ассистента) не пересекается с `billing_mode` (оплата) — разные enum/поля ([ADR-012](../../adr/ADR-012-assistant-mode-vs-billing-mode.md)).
- `codeDefaults` — без секретов (валидатор + redaction на общих логах).
