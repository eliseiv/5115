# Policy Engine — Testing

## Unit (исчерпывающе)
- Декартово произведение: {subscription: none/active/expired} × {trial_used: T/F} × {credits: 0/>0} × {byok: disabled/invalid/valid/missing} × {mode: credits/byok} → ожидаемый allow|blockReason (см. таблицу ADR-002).
- Порядок приоритета blockReason соблюдён.

## Integration (AC-6)
- Для каждого состояния: `/policy/effective.canGenerate*` == результат `/chat/run` (тот же исход allow/blocked и тот же reason).
- `reasons[]` содержит ровно причины недоступных режимов.
- Ленивое истечение: `status=active` но `expires_at<now()` → трактуется как expired.
