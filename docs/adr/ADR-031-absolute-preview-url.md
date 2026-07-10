# ADR-031 — `site.preview` возвращает АБСОЛЮТНЫЙ URL (`SERVICE_DOMAIN`)

- **Статус:** Accepted
- **Дата:** 2026-06-16
- **Связано:** [ADR-010](ADR-010-backend-hosted-preview.md) (backend-hosted preview, signed URL HMAC+TTL, threat model), [ADR-011](ADR-011-server-side-tools.md) (server-side tools `site.*`), [ADR-017](ADR-017-shared-server-traefik-deploy.md) (`SERVICE_DOMAIN` — Host-роутер Traefik, per-instance `.env`), [ADR-028](ADR-028-projectid-in-chat-list-and-server-tools-in-chat-response.md) (`serverTools[]` индикатор). Модуль [website-builder](../modules/website-builder/README.md).

> Примечание о нумерации: номер **ADR-031** ранее не существовал (предыдущий принятый — [ADR-030](ADR-030-toolcallid-in-server-tools.md); 031 свободен). Берётся первый свободный номер.

## Context

Репорт iOS-разработчика (прод): при генерации сайта ассистент отдаёт превью-ссылки с **выдуманным хостом** — `https://www.val.town/v1/preview/...`, `https://claude.site/v1/preview/...`.

Корень: `_preview` (`src/app/website/tools.py:168`) строит **относительный** путь:

```python
url = f"/v1/preview/{project.id}/{signed.token}/{entry}"
```

В `site.preview` Result (`{ url, expiresAt }`, [02-api-contracts](../modules/website-builder/02-api-contracts.md#sitepreview-utility)) `url` — относительный. Модель, дописывая markdown-ссылку в прозу ответа, **галлюцинирует базовый хост** (подставляет знакомые ей хостинги). Ссылка не открывается — ведёт на чужой домен.

Подпись/верификация/TTL preview сами по себе корректны; проблема исключительно в том, что относительный путь требует от модели «достроить» хост, и она достраивает неверно.

**Ключевой факт (разведка):** `SERVICE_DOMAIN` **уже задан** в `/opt/<instance>/.env` обоих прод-инстансов (`broadnova.shop`, `avelyraweb.shop`) и в [`.env.prod.example`](../../.env.prod.example) — он используется для Traefik Host-лейбла ([ADR-017](ADR-017-shared-server-traefik-deploy.md), [07-deployment.md §Маршрутизация](../07-deployment.md)). Но app-config (`src/app/config.py`) его **не читает**. Достаточно добавить поле в `Settings` — на проде значение подхватится после деплоя **без изменения prod-env**.

## Decision

`site.preview` возвращает **абсолютный** URL на наш домен:

```
https://<SERVICE_DOMAIN>/v1/preview/{projectId}/{token}/{path}
```

Модель копирует готовый URL **дословно** — достраивать хост ей не нужно, галлюцинация подавляется. Дополнительно описание инструмента явно требует использовать URL как есть.

**Клиентская обрезка прозы отвергнута** как альтернатива: хрупкий парсинг свободного текста, не чинит структурный `url`-контракт. Backend-фикс надёжнее и чинит источник.

### 1. Config (`src/app/config.py`)

Новое поле в `Settings`, в секции preview (рядом с `preview_*`):

```python
service_domain: str = Field(default="", alias="SERVICE_DOMAIN")
```

- **PUBLIC**, не секрет (доменное имя; уже в Traefik-лейблах и `.env.prod.example`). Под redaction не попадает.
- **Нормализация — ДА** (обязательна, чтобы URL не получился битым). Метод-хелпер `normalized_service_domain()` (или inline в `_preview`) приводит значение к голому host[:port]:
  1. `strip()` пробелов;
  2. срезать ведущий протокол `https://` / `http://` (case-insensitive);
  3. срезать **хвостовые** `/`;
  4. (доп. устойчивость) срезать ведущие `/`.
  Результат — `broadnova.shop` независимо от того, задано ли в env `broadnova.shop`, `https://broadnova.shop`, `broadnova.shop/`.
- Пустое/только-пробелы значение после нормализации → считается «не задан» → fallback (см. §2).

### 2. `_preview` (`src/app/website/tools.py`)

```python
domain = settings.normalized_service_domain()   # нормализованный host или ""
if domain:
    url = f"https://{domain}/v1/preview/{project.id}/{signed.token}/{entry}"
else:
    url = f"/v1/preview/{project.id}/{signed.token}/{entry}"   # fallback — как сейчас
```

- **Схема fallback** (при пустом `service_domain` — локальная разработка): **относительный путь**, как сейчас. **Без хардкода** `localhost`/`http`/порта (хост локально неизвестен; относительный путь не ломает текущие offline-тесты и dev).
- `expiresAt`, signed-token, `entry`, подпись — **не меняются**.
- **Никаких двойных слешей**: нормализация снимает хвостовой `/` домена; путь начинается ровно с одного `/v1/`. Протокол всегда `https://` (preview обслуживается через Traefik+TLS на проде).

### 3. Tool-описание (`src/app/chat/tools.py`, `TOOL_DESCRIPTIONS[TOOL_SITE_PREVIEW]`)

Дополнить так, чтобы подавить галлюцинацию хоста (рекомендуемая формулировка, EN — как остальные описания):

> Get a temporary signed preview URL for the current website project. Optional 'entry' selects the start file (default index.html). The returned `url` is an ABSOLUTE URL that opens directly in a browser (signed token, no authentication). Use it exactly as returned — do NOT change, shorten, or add a host/domain to it.

### 4. Безопасность

Модель угроз **не меняется** ([ADR-010](ADR-010-backend-hosted-preview.md)): тот же signed HMAC-token (`projectId|ownerUserId|exp`), тот же TTL (`PREVIEW_URL_TTL_SECONDS`), та же owner-isolation, тот же path-traversal guard и content-type allowlist. Абсолютный URL **не ослабляет** контур: авторизация по-прежнему целиком в подписи, не в хосте. preview-роутер (`GET /v1/preview/...`), верификация и TTL **не трогаются**.

Каталог из **14 инструментов** — без изменений (правится только Result-формат `url` и текст описания `site.preview`).

## Consequences

**Плюсы:**
- Ссылки превью открываются (правильный домен); галлюцинация хоста устранена в источнике.
- На проде включается **без изменения prod-env** — `SERVICE_DOMAIN` уже задан; нужен только деплой нового кода.
- Чинит и структурный `url`-контракт (не только текст ответа), в отличие от клиентской обрезки.

**Минусы / риски:**
- Появляется зависимость корректности `url` от заполненности `SERVICE_DOMAIN`. Mitigation: значение уже задано на обоих инстансах + в `.env.prod.example`; пустое → graceful fallback на относительный путь (как сейчас), не ошибка.
- Локальная разработка без `SERVICE_DOMAIN` отдаёт относительный путь — поведение прежнее, для dev допустимо ([TD-022](../100-known-tech-debt.md)).

## Alternatives

1. **Клиентская обрезка прозы (iOS).** Отвергнуто: хрупкий парсинг свободного текста, не чинит структурный `url`, дублирует логику на каждом клиенте.
2. **Хардкод базового URL в коде.** Отвергнуто: per-instance домены ([ADR-017](ADR-017-shared-server-traefik-deploy.md)), хардкод сломает мульти-инстанс.
3. **Отдельная новая env-переменная (`PREVIEW_BASE_URL`).** Отвергнуто: дублирует уже существующий `SERVICE_DOMAIN`, требует правки prod-env на обоих инстансах. `SERVICE_DOMAIN` — единственный источник истины домена.
