# Snippets — API Contracts

JWT, владелец = `sub`. Чужой сниппет → `404`.

## GET /v1/snippets
### Query
- `language` (опц.) — фильтр по языку (нормализованное значение); отсутствие = All.
- `q` (опц.) — поиск ILIKE по `title` и `code`.
- `cursor`/`limit` (опц., дефолт 30, max 100) — пагинация по `created_at`.

### Response (200)
```json
{
  "items": [
    {
      "id": "uuid",
      "title": "string",
      "language": "string",
      "tags": ["string"],
      "sourceChatId": "uuid | null",
      "createdAt": "ISO8601",
      "updatedAt": "ISO8601"
    }
  ],
  "nextCursor": "string | null"
}
```
- Список **не** включает `code` (для лёгкости); полный `code` — в `GET /{id}`. (Дефолт; если UI требует превью — добавить `codePreview` усечённый.)

## POST /v1/snippets
### Request
```json
{
  "title": "string",
  "language": "string",
  "code": "string",
  "tags": ["string"],
  "sourceChatId": "uuid (optional)"
}
```
- `extra='forbid'`. `code` ≤ 64KB, `title` ≤ 200, ≤ 20 тегов. `sourceChatId` (если задан) обязан принадлежать `sub` (иначе `403`/`404`).

### Response (201)
Полный объект сниппета (включая `code`).

## GET /v1/snippets/{id}
### Response (200)
```json
{
  "id": "uuid",
  "title": "string",
  "language": "string",
  "code": "string",
  "tags": ["string"],
  "sourceChatId": "uuid | null",
  "createdAt": "ISO8601",
  "updatedAt": "ISO8601"
}
```

## PATCH /v1/snippets/{id}
Обновление любого подмножества `title`/`language`/`code`/`tags`.
### Response (200)
Полный объект.

## DELETE /v1/snippets/{id}
### Response (200)
```json
{ "deleted": true }
```
