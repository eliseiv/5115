# Audit — Testing

## Unit
- Redactor вырезает поля `*key*`/`*token*`/`*secret*` из payload.

## Integration (AC-7)
- Каждое client-side мутирующее tool-действие (files.write/mkdir, calendar.create_events, reminders.create) → ровно одна `tool_mutation` запись.
- Каждое server-side мутирующее tool-действие (site.write_file, site.delete) → ровно одна `tool_mutation` запись, в той же транзакции, что и мутация `site_files`, без зависимости от `/chat/tool-result`.
- Каждое успешное списание → `billing_debit`.
- Записи неизменяемы: репозиторий не предоставляет update/delete.
- payload не содержит секретов (assert).
- billing_debit фиксируется в одной транзакции со списанием (нет debit без audit).
