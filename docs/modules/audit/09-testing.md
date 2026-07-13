# Audit — Testing

## Unit
- Redactor вырезает поля `*key*`/`*token*`/`*secret*` из payload.

## Integration (AC-7)
- Client-side мутирующие инструменты удалены ([ADR-063](../../adr/ADR-063-remove-client-side-calendar-reminders-files-tools.md)) — ветка `tool_mutation` для `files.*`/`calendar.*`/`reminders.*` в штатном потоке не порождается (проверяется фейковым client-side мутирующим инструментом, если тестируется механизм; старые записи сохраняются).
- Каждое server-side мутирующее tool-действие (site.write_file, site.delete) → ровно одна `tool_mutation` запись, в той же транзакции, что и мутация `site_files`, без зависимости от `/chat/tool-result`.
- Каждое успешное списание → `billing_debit`.
- Записи неизменяемы: репозиторий не предоставляет update/delete.
- payload не содержит секретов (assert).
- billing_debit фиксируется в одной транзакции со списанием (нет debit без audit).
