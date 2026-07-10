# Admin — Overview

## Назначение
Операторская/саппорт-функция: начисление кредитов пользователю вне обычного биллинг-потока подписки
(компенсации, ручные гранты, поддержка) и read-only просмотр кошелька для разбора обращений.

## Scope (этот проход)
- `POST /v1/admin/wallet/grant` — начислить `amount` кредитов пользователю `userId`, идемпотентно по `idempotencyKey`,
  с обязательным `reason`. Переиспользует существующий `WalletService.grant()` (`src/app/wallet/service.py:174`).
- `GET /v1/admin/wallet/{userId}` — баланс + последние ledger-транзакции (read-only, для саппорта).
- Изолированная admin-авторизация: `X-Admin-Token` ([ADR-009](../../adr/ADR-009-admin-token-auth.md)), зависимость `require_admin`.
- Аудит `admin_grant`, отдельный rate limit, strict validation, size-лимиты.

## Out of scope
- Любые мутации, кроме начисления кредитов (нет admin-списания, нет правки подписки/BYOK/trial, нет удаления пользователей).
- Admin-UI (только HTTP API).
- Персональная идентичность/атрибуция конкретного оператора (actor — обезличенный `admin`, [Q-009-1](../../99-open-questions.md)).
- Scope/least-privilege на уровне токена (один секрет = единственная admin-операция grant).

## Бизнес-правила
- BR-ADM-1: admin **не пользователь системы** — `require_admin` не создаёт строку `users` для actor'а, не запускает
  lazy-provisioning ([ADR-007](../../adr/ADR-007-lazy-user-provisioning.md)), не читает/не трогает `users.trial_used`.
- BR-ADM-2: начисление идемпотентно по `idempotencyKey` (через `WalletService.grant`, unique index `ux_ledger_idempotency`);
  повторный вызов с тем же ключом и payload → тот же `ledgerTxId`, `idempotentReplay=true`, без повторного начисления.
- BR-ADM-3: `reason` обязателен и пишется в audit `admin_grant` (и в `ledger_transactions.meta`, без секретов).
- BR-ADM-4: целевой `userId` **должен существовать** — admin-grant не создаёт пользователей (см. 03-architecture §Несуществующий userId).
