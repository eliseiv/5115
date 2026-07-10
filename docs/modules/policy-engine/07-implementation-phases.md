# Policy Engine — Implementation Phases

| Phase | Задача | Зависит от |
|---|---|---|
| PE-1 | `evaluate(state, mode)` чистая функция + типы (ADR-002). | — |
| PE-2 | Параметризованные unit-тесты полной таблицы переходов. | PE-1 |
| PE-3 | `PolicyStateLoader` (чтение subscription/wallet/byok/users). | DB repos |
| PE-4 | `/v1/policy/effective` endpoint + reasons[]. | PE-1, PE-3 |
| PE-5 | Опциональный Redis-кэш effective + инвалидация. | PE-4 |

> PE-1/PE-2 не зависят ни от чего — реализуются первыми, дают фундамент для Orchestrator.
