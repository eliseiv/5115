# Preferences — RBAC

- Роль `user`. `GET`/`PATCH /v1/preferences` оперируют строкой для `sub`.
- Нет admin-операций.
- `codeDefaults` не должен содержать секреты (валидация + redaction).
