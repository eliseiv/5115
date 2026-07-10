# Snippets — Testing

## Unit
- Нормализация `language`; фильтр `All` = без фильтра.
- Лимиты (`code` ≤ 64KB, `title` ≤ 200, ≤ 20 тегов).

## Integration
- `GET` — фильтр по языку, поиск `q` (title+code), пагинация; список без `code`.
- `POST` с `sourceChatId` своего чата → `201`; чужого → `403`/`404`.
- `GET /{id}` возвращает полный `code`; чужой → `404`.
- `PATCH`/`DELETE` — изоляция владельца.
- Удаление `sourceChatId`-чата → сниппет жив, `sourceChatId` → NULL.
