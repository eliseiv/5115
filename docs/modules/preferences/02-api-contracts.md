# Preferences — API Contracts

JWT, владелец = `sub`.

## GET /v1/preferences
### Response (200)
```json
{
  "defaultAssistantMode": "chat | code",
  "defaultDialogMode": "smart | deep_thinking | study_learn | search",
  "notificationsEnabled": false,
  "codeDefaults": { }
}
```
- Если строки `user_preferences` нет — возвращаются дефолты (`chat` / `smart` / `false` / `{}`). Дефолт `notificationsEnabled=false` ([ADR-032](../../adr/ADR-032-notifications-enabled-default-false.md)): privacy-by-default, iOS включает push через `PATCH` после системного разрешения. Существующие строки сохраняют сохранённое значение.
- `defaultDialogMode` ([ADR-055 §3](../../adr/ADR-055-dialog-mode.md)) — дефолтный режим диалога, используется при создании сессии, если `dialogMode` не передан в `/chat/run`. Дефолт `smart` при отсутствии строки. Ортогонален `defaultAssistantMode` (тип ассистента) и billing_mode.

## PATCH /v1/preferences
Частичное обновление (любое подмножество полей).

### Request
```json
{
  "defaultAssistantMode": "chat | code",
  "defaultDialogMode": "smart | deep_thinking | study_learn | search",
  "notificationsEnabled": true,
  "codeDefaults": { }
}
```
- `extra='forbid'`. Хотя бы одно поле. `defaultAssistantMode` ∈ {chat, code}, иначе `422`. `defaultDialogMode` ∈ {smart, deep_thinking, study_learn, search}, иначе `422` (валидация членства, по образцу `defaultAssistantMode`). `codeDefaults` ≤ 8KB сериализованного JSON.
- Upsert: создаёт строку при отсутствии, обновляет заданные поля.
- **Provider-gate `dialog_mode` проверяется НА СОЗДАНИИ СЕССИИ, а не при сохранении preference ([ADR-055 §4](../../adr/ADR-055-dialog-mode.md)).** PATCH валидирует только членство в enum — сохранить `defaultDialogMode=search` (или `deep_thinking`/`study_learn`) допустимо на **любом** инстансе, включая anthropic. Ограничение по провайдеру срабатывает позже: при `POST /v1/chat/run`, где создаётся сессия с этим режимом на anthropic-инстансе → **`422 unsupported_dialog_mode`** ([ADR-059](../../adr/ADR-059-openai-default-provider.md)). Preference — это лишь дефолт-предпочтение, не гарантия доступности режима на инстансе.

### Response (200)
Полный текущий объект preferences (как GET).
