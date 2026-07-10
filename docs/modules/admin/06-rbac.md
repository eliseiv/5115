# Admin — RBAC

## Принципал
- `admin` — обезличенный оператор, авторизуется изолированным `X-Admin-Token` ([ADR-009](../../adr/ADR-009-admin-token-auth.md)).
  Не имеет `userId`/`sub`, не является пользователем системы.

## Правила
- Доступ к `/v1/admin/*` — **только** при валидном `X-Admin-Token` (зависимость `require_admin`). Иначе `401`.
- Пользовательский JWT (`Authorization: Bearer`) **не** даёт доступа к admin-роутам и не является фактором авторизации на них.
- Admin-токен **не** даёт доступа к пользовательским ресурсам через пользовательские эндпоинты (`/v1/chat/*`, `/v1/wallet`, …) —
  там по-прежнему требуется JWT и сверка `sub`.
- Эскалация невозможна: разные секреты, заголовки, зависимости (ADR-009 §4).
- Admin действует **над** `userId` из тела (`grant`)/пути (`get-wallet`) — это легитимно **только** на admin-роутах;
  на пользовательских роутах действие за другого `userId` запрещено (`403`, [05-security.md](../../05-security.md)).

## Изоляция инвариантов
- `require_admin` не запускает provisioning ([ADR-007](../../adr/ADR-007-lazy-user-provisioning.md)) и не трогает `trial`.
- Единственная мутирующая admin-операция — начисление кредитов (`grant`). Admin-списания, правки подписки/BYOK/trial,
  удаления пользователей — отсутствуют (out of scope, [00-overview.md](00-overview.md)).

## Аудит
- Каждый `grant` → audit-событие `admin_grant` (actor=admin, `userId`, `amount`, `reason`, `idempotencyKey`, `ledgerTxId`).
  Секрет `X-Admin-Token` в audit/логи не попадает.
