# Audit — Overview

## Scope
- Append-only запись в `audit_logs` (внутренний API `record(event)`).
- Фиксация: мутирующие tool-действия, billing (debit/credit), policy decisions, byok changes, subscription changes.
- Гарантия покрытия AC-7.

## Out of scope
- Аналитика/дашборды (это Observability/внешние системы).
- Изменение/удаление записей (append-only).

## Принцип
Никто, кроме Audit, не пишет в `audit_logs`. Никакой код не делает UPDATE/DELETE. Жёсткая БД-защита — потенциальный [TD-001](../../100-known-tech-debt.md#known-tech-debt).
