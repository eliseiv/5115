# Token Purchase — RBAC

- Роль `user`. Покупка начисляет кредиты строго на `sub` (verify-результат + Wallet.grant для `sub`).
- `userId` ≠ `sub` → `403`.
- Нет admin-операций (admin-grant — отдельный модуль admin, ADR-009).
- StoreKit payload — в redaction allowlist (не логируется).
