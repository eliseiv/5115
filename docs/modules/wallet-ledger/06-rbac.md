# Wallet / Ledger — RBAC

## Роль
- `user` — видит и тратит только свой кошелёк.

## Правила
- `GET /v1/wallet` — userId из JWT `sub`.
- `POST /v1/wallet/consume` — `userId` == `sub` (enforced Gateway); списание только со своего кошелька.
- `grant` — внутренний вызов (Subscription), не доступен клиенту напрямую.
- Нет доступа к чужим транзакциям.
