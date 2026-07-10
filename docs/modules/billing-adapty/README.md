# Module: Billing — Adapty

- Статус: Спроектирован, ожидает реализации
- Ответственность: приём серверного вебхука Adapty (`POST /v1/billing/adapty/webhook`), идемпотентное обновление подписки и грант кредитов по тиру продукта. Основной путь биллинга по подпискам ([ADR-029](../../adr/ADR-029-adapty-subscription-webhook.md)).

## Документы
- [00-overview.md](00-overview.md) · [01-context.md](01-context.md) · [02-api-contracts.md](02-api-contracts.md) · [03-architecture.md](03-architecture.md) · [04-data-model.md](04-data-model.md) · [06-rbac.md](06-rbac.md) · [07-implementation-phases.md](07-implementation-phases.md) · [08-observability.md](08-observability.md)

## DoD
- Эндпоинт авторизуется статическим bearer-секретом (constant-time); неверный/нет токена → 401; секрет не задан → 500.
- После авторизации любое тело (пустое/не-JSON/неполное/неизвестное событие/дубликат) → `2xx` (Adapty сохраняет вебхук и не зацикливает ретраи). `5xx` только при реальном сбое.
- GRANTING (`trial_started`/`subscription_started`/`subscription_renewed`/`access_level_updated`@premium) → `status=active` + идемпотентный грант по тиру; EXPIRING (`subscription_expired`/`subscription_cancelled`/`access_level_updated`@is_active=false) → `status=expired`, кредиты не трогаются; **NOOP** (`*_renewal_cancelled`) → доступ НЕ отзывается, кредиты не трогаются ([ADR-047](../../adr/ADR-047-adapty-real-payload-format-and-grant-idempotency.md)).
- Идемпотентность ([ADR-047](../../adr/ADR-047-adapty-real-payload-format-and-grant-idempotency.md)): дедуп события по `profile_event_id`; **грант — один на период покупки** (`adapty-txn:{transaction_id}`), без двойного начисления при нескольких событиях одной покупки.
- Audit-событие `adapty_subscription` пишется через `assert_no_secrets`.

## Границы (см. [00-overview.md](00-overview.md))
- **НЕ** покрывает consumable-пакеты токенов (остаются на `/v1/tokens/purchase`, [ADR-015](../../adr/ADR-015-consumable-token-iap.md), [Q-029-1](../../99-open-questions.md)).
- **НЕ** ломает `/v1/subscription/sync` (StoreKit JWS) — он остаётся, но источник истины по подпискам = Adapty. Ретирование `sync` отложено ([Q-029-2](../../99-open-questions.md), [TD-021](../../100-known-tech-debt.md)).

## Changelog
- 2026-06-12: проектирование (architect), [ADR-029](../../adr/ADR-029-adapty-subscription-webhook.md). Новая таблица `adapty_webhook_events` (миграция `0008`), env `ADAPTY_WEBHOOK_SECRET` / `ADAPTY_PRODUCT_TOKENS` / `ADAPTY_SUBSCRIPTION_TOKENS_GRANT`. Заведены [Q-029-1/2/3](../../99-open-questions.md), [TD-020/021](../../100-known-tech-debt.md).
- 2026-06-30: наблюдаемость (architect), [ADR-046](../../adr/ADR-046-adapty-webhook-outcome-logging.md). Структурный лог исхода `handle()` (`"adapty_webhook_outcome"`, allowlist `result`/`reason`/`eventType`/`eventId`/`customerUserId`, уровни INFO/WARNING/DEBUG) — закрывает слепое пятно инцидента (промо-подписка → `ignored` без лога причины). Новый [08-observability.md](08-observability.md), Фаза 7 в [07](07-implementation-phases.md). Без миграции/env/контракта. [Q-029-3](../../99-open-questions.md) частично адресован (наблюдаемость), маппинг событий остаётся открытым.
- 2026-06-30: исправление парсера/маппинга/идемпотентности по реальным payload'ам (architect), [ADR-047](../../adr/ADR-047-adapty-real-payload-format-and-grant-idempotency.md). `event_id`←`profile_event_id`; дефенсивный парсинг `event_properties.*` + новые поля (`transaction_id`/`is_active`/`access_level_id`/`will_renew`); маппинг `classify_event` GRANTING/EXPIRING/**NOOP** (`*_renewal_cancelled` доступ НЕ отзывает; `access_level_updated` условно по `is_active`/`premium`); **грант идемпотентен по `adapty-txn:{transaction_id}`** (один грант на период, дедуп события — отдельно по `profile_event_id`); грант на trial. Фаза 8 в [07](07-implementation-phases.md). Без миграции/env, контракт/HTTP/схема неизменны. [Q-029-3](../../99-open-questions.md) — маппинг определён, верификация wire-формата по прод-логам; новый [Q-047-1](../../99-open-questions.md) (персист `will_renew`, отложено).
