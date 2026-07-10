# ADR-045 — Per-path transport body-limit для загрузки workspace-файлов (фикс 413 на upload)

- **Статус:** Accepted
- **Дата:** 2026-06-25
- **Тип:** implementation-ADR (расширяет существующее проектирование, без нового модуля/миграции/биллинга).
- **Расширяет:** [ADR-036 §4/§6](ADR-036-workspaces-implementation.md) (workspace-файлы, `WORKSPACE_FILE_MAX_BYTES`=8 MB), переиспользует механику per-route body-limit из [ADR-020](ADR-020-inline-base64-attachments-mvp.md) / `ATTACHMENT_REQUEST_BODY_LIMIT`.
- **Связано:** [05-security.md §Повышенный transport-лимит](../05-security.md), [modules/api-gateway/03-architecture.md](../modules/api-gateway/03-architecture.md), [modules/workspaces/02-api-contracts.md](../modules/workspaces/02-api-contracts.md), [TD-017](../100-known-tech-debt.md) (Content-Length-зависимость guard'а), [TD-004](../100-known-tech-debt.md) (калибровка лимитов), [TD-027](../100-known-tech-debt.md) (BYTEA-хранение workspace-файлов).

## Контекст

Подтверждено живой диагностикой на проде (orvianix): `POST /v1/workspaces/{workspaceProjectId}/files` возвращает `413 {"error":{"code":"payload_too_large","message":"request body exceeds limit"}}` для любого файла, чьё тело запроса превышает ~512 KB (то есть реальный файл крупнее ~375 KB с учётом base64-раздувания ~33%).

**Корневая причина (подтверждена чтением кода):**
- `src/app/api_gateway/middleware.py` — `SizeLimitMiddleware._limit_for(path)` применяет общий `settings.size_limit_body` (`SIZE_LIMIT_BODY`, дефолт `512*1024` = 512 KB, `config.py:206`) ко **всем** путям и **исключает только** `/v1/chat/run` (точное сравнение `path == self._CHAT_RUN_PATH`), которому отдаёт `attachment_request_body_limit` (`ATTACHMENT_REQUEST_BODY_LIMIT`, дефолт 12 MB, `config.py:231-232`).
- Путь загрузки workspace-файлов `/v1/workspaces/{id}/files` под исключение **не попал** → тело режется на 512 KB **в gateway**, до того как отработает `src/app/workspaces/text_extract.py::validate_and_extract`, который по [ADR-036 §4](ADR-036-workspaces-implementation.md) разрешает `WORKSPACE_FILE_MAX_BYTES`=8 MB на файл.
- Итог: заявленный ADR-036 лимит 8 MB **фактически недостижим** — реальный потолок ~375 KB.

Это полный аналог уже решённой для attachment ситуации ([ADR-020](ADR-020-inline-base64-attachments-mvp.md)): inline base64 крупного файла превышает общий 512 KB-cap, поэтому транспортный лимит **точечно** поднят только для роута, принимающего такой payload.

**Разведка скоупа (подтверждено чтением):**
- Единственный отдельный base64-upload endpoint помимо `/v1/chat/run`, страдающий проблемой, — `POST /v1/workspaces/{id}/files` (`src/app/api_gateway/routers/workspaces.py:200`, схема `WorkspaceFileUploadRequest`, `data: base64`).
- Website-builder (`site.*`): загрузка файлов сайта идёт **server-side tools внутри `/v1/chat/run`** (`src/app/website/tools.py`), отдельного file-upload HTTP-endpoint нет → уже под 12 MB лимитом `/v1/chat/run`, отдельной правки не требует.
- Прочие POST (`/v1/auth/*` Apple token, `/v1/byok/*` ключ, `/v1/billing/adapty/webhook`, `subscription`/`token_purchase`/`wallet`) принимают мелкие тела (токен/ключ/JSON-событие) — общий 512 KB для них корректен и сохраняется.

## Решение

### 1. Отдельный конфиг `WORKSPACE_REQUEST_BODY_LIMIT` (дефолт 12 MB)

Ввести **отдельное** поле конфига, **не переиспользовать** `attachment_request_body_limit`:

```python
# config.py, в блоке Workspaces (рядом с workspace_file_max_bytes)
workspace_request_body_limit: int = Field(
    default=12 * 1024 * 1024, alias="WORKSPACE_REQUEST_BODY_LIMIT"
)
```

**Обоснование выбора отдельного поля (а не reuse `ATTACHMENT_REQUEST_BODY_LIMIT`):**
- Лимиты workspace-файлов и chat-attachments должны эволюционировать **независимо**: per-file caps уже различаются (`WORKSPACE_FILE_MAX_BYTES`=8 MB на один файл vs `ATTACHMENT_TOTAL_BYTES`=10 MB на запрос с возможными несколькими вложениями), и операторская калибровка ([TD-004](../100-known-tech-debt.md)) одного не должна молча менять другой.
- Семантика разная: workspace upload — **один файл** на запрос; `/chat/run` — **до `ATTACHMENT_MAX_COUNT`=10** вложений на запрос.
- Принцип ADR-020 «минимальная поверхность приёма крупного payload, точечно на роут» сохраняется: каждый поднятый лимит явен и привязан к своему роуту.

**Инвариант источника истины (обязателен для backend):**
```
workspace_request_body_limit ≥ ceil(workspace_file_max_bytes * 4 / 3) + JSON_OVERHEAD
```
где `workspace_file_max_bytes * 4/3` — размер base64-представления 8 MB файла (≈ 10.67 MB), а `JSON_OVERHEAD` — запас на JSON-обёртку (`{"type","mediaType","filename","data":"..."}`, экранирование, заголовки полей; рекомендованный запас ≥ 256 KB). Дефолт 12 MB удовлетворяет: 8 MB × 4/3 ≈ 10.67 MB + 12 MB − 10.67 MB ≈ 1.33 MB запаса > 256 KB. **Один источник истины для per-file потолка — `WORKSPACE_FILE_MAX_BYTES`;** `WORKSPACE_REQUEST_BODY_LIMIT` производен от него и обязан оставаться ≥ инварианта при любой калибровке (симметрично связи `ATTACHMENT_MAX_BYTES_DOCUMENT` ↔ `ATTACHMENT_REQUEST_BODY_LIMIT` для `/chat/run`). Инвариант фиксируется комментарием в `config.py` и проверяется тестом (qa).

### 2. Правило сопоставления пути в middleware (префикс + суффикс)

Текущее точное сравнение `path == "/v1/chat/run"` не подходит для пути с параметром `/v1/workspaces/{id}/files`. Ввести явное правило:

```python
def _is_workspace_files_path(path: str) -> bool:
    return path.startswith("/v1/workspaces/") and path.endswith("/files")
```

**Точное правило сопоставления (зафиксировано как контракт):**
- Поднятый лимит `workspace_request_body_limit` применяется к пути, который **одновременно** начинается с `/v1/workspaces/` **и** заканчивается на `/files`.
- Это матчит **именно** upload-эндпоинт `POST /v1/workspaces/{id}/files` и НЕ затрагивает:
  - CRUD workspace (`/v1/workspaces`, `/v1/workspaces/{id}`) — у них нет суффикса `/files` → остаются на 512 KB (корректно, тела мелкие);
  - удаление файла `DELETE /v1/workspaces/{id}/files/{file_id}` — путь оканчивается на `/{file_id}`, **не** на `/files` → 512 KB (корректно, тело пустое);
  - `GET /v1/workspaces/{id}/files` (список) — оканчивается на `/files`, **но** GET не несёт тела (`Content-Length` отсутствует/0) → повышенный лимит для него безвреден.
- **Замечание по методу:** правило сопоставления — по **пути** (метод-агностично), как и существующее исключение `/v1/chat/run`. `GET /v1/workspaces/{id}/files` тоже попадёт под повышенный лимит, но это не расширяет поверхность атаки: GET-тело пустое. Усложнять матч проверкой метода **не требуется** (минимизация изменений; единственный путь с непустым телом под этим суффиксом — POST upload).

`_limit_for` приобретает вид:
```python
def _limit_for(self, path: str) -> int:
    if path == self._CHAT_RUN_PATH:
        return self._chat_run_limit
    if path.startswith(self._WORKSPACES_PREFIX) and path.endswith(self._FILES_SUFFIX):
        return self._workspace_files_limit
    return self._limit
```
Константы (`_WORKSPACES_PREFIX="/v1/workspaces/"`, `_FILES_SUFFIX="/files"`, `_workspace_files_limit = settings.workspace_request_body_limit`) определяются в `SizeLimitMiddleware`.

### 3. Memory-DoS guard сохраняется

Повышенный лимит применяется **только** к upload-пути workspace-файлов; общий 512 KB остаётся для всех прочих роутов. Поверхность приёма крупного payload расширяется ровно на один роут (как было с `/v1/chat/run`). Прикладные проверки `validate_and_extract` (size-cap **до** base64-decode, magic-bytes, PDF page-guard) остаются первой линией защиты от memory-DoS и продолжают резать payload до `WORKSPACE_FILE_MAX_BYTES`=8 MB **внутри** поднятого транспортного окна. Остаточная зависимость guard'а от заголовка `Content-Length` — прежняя ([TD-017](../100-known-tech-debt.md)), новым решением не усугубляется и не закрывается.

### 4. Обратная совместимость

- Существующие мелкие загрузки workspace-файлов (< 512 KB) продолжают работать без изменений.
- Все прочие endpoints не затрагиваются (общий 512 KB неизменен).
- Без миграции БД, без изменения публичного контракта `POST /v1/workspaces/{id}/files` (поля/валидации/коды ответов те же; меняется только верхняя граница транспортного тела — теперь реально достижим заявленный 8 MB).
- Дефолт `WORKSPACE_REQUEST_BODY_LIMIT`=12 MB можно переопределить через env на инстансе (как `ATTACHMENT_REQUEST_BODY_LIMIT`).

## Последствия

**Положительные:**
- Заявленный ADR-036 лимит 8 MB на workspace-файл становится **фактически достижим** (баг 413 устранён).
- Лимиты workspace и attachment эволюционируют независимо; явный инвариант связывает транспортный лимит с per-file потолком (нет «магических» рассинхронов при калибровке).
- Правило сопоставления пути точное — другие `/v1/workspaces/*` роуты не теряют защиту 512 KB.

**Отрицательные / принятые:**
- Поверхность приёма ≤12 MB payload расширяется на один дополнительный роут (POST upload). Принято: симметрично `/v1/chat/run`, прикладная валидация режет до 8 MB до декодирования.
- `_limit_for` усложняется одним правилом префикс/суффикс вместо точного сравнения. Покрывается тестами (qa).
- `GET /v1/workspaces/{id}/files` формально попадает под повышенный лимит (безвредно — пустое тело). Не митигируется ради простоты.

## Альтернативы (отклонены)

- **Переиспользовать `ATTACHMENT_REQUEST_BODY_LIMIT` для workspace-upload.** Отклонено: связывает несвязанные лимиты, операторская калибровка одного молча меняет другой; семантика (1 файл vs ≤10 вложений) различна. См. §1.
- **Поднять общий `SIZE_LIMIT_BODY` до 12 MB.** Отклонено: глобально расширяет поверхность memory-DoS на ВСЕ роуты (включая auth/byok/webhook), нарушает принцип точечного повышения ADR-020 / [05-security.md](../05-security.md).
- **Точный матч полного шаблона пути (регэксп `^/v1/workspaces/[^/]+/files$`).** Отклонено в пользу `startswith+endswith`: проще, дешевле, без риска ReDoS, и точно так же не задевает CRUD/delete-file/`{file_id}` пути (delete оканчивается на `/{file_id}`, не на `/files`). Поведение эквивалентно для реальных путей роутера.
- **Матч с проверкой HTTP-метода (только POST).** Отклонено как избыточное: единственный путь с непустым телом под суффиксом `/files` — POST upload; GET-список тело не несёт. Усложнение без выгоды.

## Скоуп backend (ТЗ)

**Файлы:**
1. `src/app/config.py` — добавить поле `workspace_request_body_limit` (alias `WORKSPACE_REQUEST_BODY_LIMIT`, дефолт `12 * 1024 * 1024`) в блок Workspaces; комментарием зафиксировать инвариант `workspace_request_body_limit ≥ workspace_file_max_bytes * 4/3 + JSON-запас (≥256 KB)`.
2. `src/app/api_gateway/middleware.py` — в `SizeLimitMiddleware`: добавить константы `_WORKSPACES_PREFIX="/v1/workspaces/"`, `_FILES_SUFFIX="/files"`, прочитать `self._workspace_files_limit = settings.workspace_request_body_limit` в `__init__`; расширить `_limit_for` правилом `startswith(_WORKSPACES_PREFIX) and endswith(_FILES_SUFFIX)`. Обновить docstring класса (исключение теперь два роута: `/v1/chat/run` + workspace files upload).

**Не трогать:** прикладную валидацию `workspaces/text_extract.py`, схемы, `/v1/chat/run`-ветку, общий `SIZE_LIMIT_BODY`, никакие другие роуты.

**Инвариант (тест, qa):** middleware-лимит для пути `/v1/workspaces/{id}/files` ≥ `workspace_file_max_bytes * 4/3`; файл ровно `WORKSPACE_FILE_MAX_BYTES` (8 MB) в base64 проходит gateway (не 413 на транспорте); файл, чьё тело > `WORKSPACE_REQUEST_BODY_LIMIT`, → 413; CRUD-путь `/v1/workspaces/{id}` с телом > 512 KB → 413 (общий лимит сохранён); `/v1/chat/run` поведение неизменно.
