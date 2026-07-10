# Attachments — RBAC

- Роль `user`. Upload/GET/DELETE и резолв в `/chat/run` ограничены вложениями `sub`.
- Чужое вложение → `404` (GET/DELETE) или `403`/`404` (резолв в chat).
- Нет admin-операций.
- Сырой бинарь/`extracted_text` не выдаются наружу как файл — только метаданные.
