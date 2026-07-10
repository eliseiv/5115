# Admin — Context

## Зависимости
| Зависит от | Зачем |
|---|---|
| Wallet / Ledger | `WalletService.grant()` (начисление, идемпотентно), `get_wallet_view()` (read-only баланс + транзакции) |
| Audit | запись события `admin_grant` |
| API Gateway (частично) | размещение роутера `/v1/admin/*`, redaction `X-Admin-Token`, отдельный rate limit. **НЕ** использует `get_current_user`/provisioning |
| PostgreSQL | через Wallet (wallets, ledger_transactions, users-проверка) |

## Кто зависит
- Внешние операторы/саппорт-инструменты (вызывают admin-API с `X-Admin-Token`).

## Изоляция от пользовательского потока
- Admin-роуты используют **только** зависимость `require_admin` ([ADR-009](../../adr/ADR-009-admin-token-auth.md)).
- НЕ проходят через `get_current_user` → нет lazy-provisioning, нет сверки `sub`, нет создания строки `users` для actor.
- Пользовательский `Authorization: Bearer <JWT>` на admin-роутах не является фактором авторизации.

## Связанные ADR / вопросы
- [ADR-009](../../adr/ADR-009-admin-token-auth.md) — изолированный admin-токен (`X-Admin-Token`), ротация, невозможность эскалации.
- [ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md) — семантика grant (целые кредиты, идемпотентность по ключу).
- [ADR-007](../../adr/ADR-007-lazy-user-provisioning.md) — admin **не** запускает provisioning.
- [Q-009-1](../../99-open-questions.md) — атрибуция оператора / переход на admin-JWT при росте требований (не блокер).
