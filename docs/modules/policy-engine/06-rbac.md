# Policy Engine — RBAC

## Роль
- `user` — видит только свои эффективные права.

## Правила
- `/v1/policy/effective` — userId из JWT `sub`; нельзя запросить чужие права.
- Policy Engine не выполняет мутаций → нет write-прав.
