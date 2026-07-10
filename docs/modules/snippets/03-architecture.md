# Snippets — Architecture

## Размещение
Пакет `src/app/snippets/`: репозиторий над `snippets` + use-cases (CRUD/list) + роутер `/v1/snippets/*`.

## Фильтр и поиск
- `language` — точное совпадение по нормализованному значению (нормализация при записи и в фильтре). Индекс `ix_snippets_user_language`.
- `q` — `title ILIKE %q%` OR `code ILIKE %q%`. На старте без полнотекстового индекса; при росте — GIN (TD по сигналу, как TD-002).

## sourceChatId
- При создании из чата клиент передаёт `sourceChatId`. Используется действием «Open in Chat». При удалении чата FK `ON DELETE SET NULL` сохраняет сниппет.

## Инварианты
- Все запросы скоупятся `WHERE user_id = :sub`.
- Список не отдаёт `code` (экономия трафика); полный код — по `GET /{id}`.
- «Open in Chat»/«Add to Project» — клиентские действия поверх существующих эндпоинтов (`/chat/run`), не требуют новых backend-роутов.
