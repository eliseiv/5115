# Preferences — Testing

## Unit
- Дефолты при отсутствии строки (chat/true/{}).
- Частичный PATCH обновляет только переданные поля (остальные не сбрасываются).
- Невалидный `defaultAssistantMode` → `422`; `codeDefaults` > 8KB → `422`/`413`.

## Integration
- GET до PATCH → дефолты; после PATCH → сохранённое.
- Orchestrator-интеграция: `/chat/run` без `assistantMode` берёт `default_assistant_mode`; с явным `assistantMode` — игнорирует preferences.
- Изоляция по `sub`.
