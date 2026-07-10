# Attachments — Architecture

## Размещение
Пакет `src/app/attachments/`: репозиторий над `attachments` + use-cases (upload/get/delete) + text-extractor + роутер `/v1/attachments`. Резолв в content-блоки — утилита, вызываемая chat-orchestrator.

## Поток upload
1. Multipart-приём; transport size-guard по `kind`.
2. Определение `media_type` по magic bytes (не по расширению/заголовку клиента).
3. Allowlist-проверка; вне списка → `422`.
4. Для document — извлечение `extracted_text` (PDF-парсер из [02-tech-stack.md](../../02-tech-stack.md); text — как есть), усечение до `ATTACHMENT_EXTRACT_MAX_CHARS`.
5. Сохранение байтов (BYTEA) + метаданные; `session_id=NULL`.

## Резолв в Anthropic content (вызывается orchestrator)
- `resolve_attachments(user_id, [ids]) -> [content_block]`. Проверка владельца, сборка `image`/`document`/text-блоков. In-memory; байты не логируются.

## Хранилище
- БД BYTEA на старте (как `site_files`). Общий [TD-009](../../100-known-tech-debt.md) — миграция в object-storage сохранит контракты модуля (интерфейс репозитория спроектирован под замену backend'а хранения).
- Orphan-очистка (session_id IS NULL, старше TTL) — [TD-010](../../100-known-tech-debt.md), на старте без джоба.

## Инварианты
- Байты/`extracted_text` наружу как файл не отдаются (только метаданные); используются backend'ом для Anthropic.
- Изоляция владельца на всех путях (`user_id == sub`).
- Биллинг не меняется ([ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md)); vision usage фиксируется в `chat_steps.usage`/`meta` для аудита.
