# Attachments — Testing

## Unit
- Magic-bytes detection: подмена расширения (png-байты с .pdf-именем) определяется как image/png; вне allowlist → reject.
- Size-guard по `kind` (image vs document лимиты).
- extract_text: PDF → текст (усечение до лимита); text/plain → как есть; image → нет extracted_text.

## Integration
- `POST /v1/attachments` — успешная загрузка image/PDF; `413` на превышении; `422` на media_type вне allowlist.
- `GET`/`DELETE` — изоляция владельца (`404` на чужое).
- `/chat/run` с `attachments[]` — резолв image→vision block, document→document/text; чужой attachment → `403`/`404`; > 10 вложений → `422`.
- Биллинг при vision-сообщении — ровно 1 кредит (ADR-006 без изменений).

## Security
- Байты/extracted_text не появляются в логах/audit/трейсах (redaction).
- Нет публичной отдачи сырого бинаря.
