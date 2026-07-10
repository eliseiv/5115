# Wallet / Ledger — Testing

## Unit
- Расчёт нового баланса; отказ при amount > balance.

## Integration (реальный PostgreSQL, AC-3)
- Конкурентные `consume` с одним idempotency key (полем `requestId`; для chat-debit — один `messageStepId`) параллельно → ровно одно списание, остальные идемпотентно возвращают тот же txId.
- Один и тот же idempotency key (`requestId`/`messageStepId`), разный `amount` → 409.
- Re-entry message-шага: `/chat/run` → несколько `/chat/tool-result` с одним `messageStepId` → ровно один debit на финальном assistant_message.
- `consume` при balance < amount → 409 insufficient, баланс не изменён, не отрицателен.
- DB CHECK: попытка отрицательного баланса невозможна.
- `grant` идемпотентен.
- `GET /v1/wallet` отдаёт корректные lastTransactions в порядке убывания.
- audit billing_debit создаётся на каждое успешное списание (AC-7).
