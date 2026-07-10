"""Wallet service: atomic, idempotent consume/grant (ADR-005, ADR-006; AC-3).

consume: INSERT ledger ON CONFLICT DO NOTHING + conditional UPDATE balance >= amount
+ DB CHECK (balance >= 0). Idempotency by (user_id, idempotency_key). For chat-debit the
idempotency_key is messageStepId (NOT gateway requestId). grant: same shape, type=credit,
idempotency by transactionId of the subscription period.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import (
    EVENT_BILLING_CREDIT,
    EVENT_BILLING_DEBIT,
    AuditEvent,
    AuditService,
)
from app.errors import (
    ConflictError,
    ForbiddenError,
    InsufficientCreditsError,
    SessionNotFoundError,
)
from app.models import LedgerTransaction, Wallet
from app.observability.metrics import wallet_debit_total


@dataclass(frozen=True)
class ConsumeResult:
    new_balance: int
    ledger_tx_id: uuid.UUID
    idempotent_replay: bool


@dataclass(frozen=True)
class GrantResult:
    new_balance: int
    ledger_tx_id: uuid.UUID
    idempotent_replay: bool


class WalletService:
    def __init__(self, session: AsyncSession, audit: AuditService) -> None:
        self._session = session
        self._audit = audit

    async def _ensure_wallet(self, user_id: uuid.UUID) -> None:
        """Idempotent auto-provisioning of the wallet row (wallet-ledger/03)."""
        await self._session.execute(
            text(
                "INSERT INTO wallets (user_id, balance) VALUES (:uid, 0) "
                "ON CONFLICT (user_id) DO NOTHING"
            ),
            {"uid": str(user_id)},
        )

    async def _existing_tx(
        self, user_id: uuid.UUID, idempotency_key: str
    ) -> LedgerTransaction | None:
        row: LedgerTransaction | None = await self._session.scalar(
            select(LedgerTransaction).where(
                LedgerTransaction.user_id == user_id,
                LedgerTransaction.idempotency_key == idempotency_key,
            )
        )
        return row

    async def _current_balance(self, user_id: uuid.UUID) -> int:
        wallet = await self._session.scalar(select(Wallet).where(Wallet.user_id == user_id))
        return int(wallet.balance) if wallet is not None else 0

    async def _validate_session(self, user_id: uuid.UUID, session_id: uuid.UUID) -> None:
        """Validate sessionId before any FK-dependent op (wallet-ledger/02; robustness vs 500).

        A bogus sessionId would otherwise hit a FK violation on audit_logs.session_id and surface
        as a 500. We resolve the owning user_id up front: missing → 404 session_not_found; owned by
        another user → 403. Parameterized query; runs before idempotency/balance checks.
        """
        owner = await self._session.scalar(
            text("SELECT user_id FROM chat_sessions WHERE id = :sid"),
            {"sid": str(session_id)},
        )
        if owner is None:
            raise SessionNotFoundError("session not found")
        if uuid.UUID(str(owner)) != user_id:
            raise ForbiddenError("session does not belong to user")

    async def consume(
        self,
        *,
        user_id: uuid.UUID,
        amount: int,
        idempotency_key: str,
        meta: dict[str, Any],
        session_id: uuid.UUID | None = None,
    ) -> ConsumeResult:
        """Atomic, idempotent debit. amount > 0. See ADR-005."""
        if amount <= 0:
            raise ConflictError("amount must be positive")
        # wallet-ledger/02: validate sessionId BEFORE idempotency/balance and any FK-dependent
        # write (debit + audit billing_debit). Prevents a 500 from a FK violation on a bogus id.
        if session_id is not None:
            await self._validate_session(user_id, session_id)
        await self._ensure_wallet(user_id)

        # Idempotency source of truth: unique index on (user_id, idempotency_key).
        inserted_id = await self._session.scalar(
            text(
                "INSERT INTO ledger_transactions (user_id, type, amount, meta, idempotency_key) "
                "VALUES (:uid, 'debit', :amount, CAST(:meta AS JSONB), :key) "
                "ON CONFLICT (user_id, idempotency_key) DO NOTHING "
                "RETURNING id"
            ),
            {
                "uid": str(user_id),
                "amount": amount,
                "meta": _json(meta),
                "key": idempotency_key,
            },
        )

        if inserted_id is None:
            # Idempotent replay: same key already exists. Verify payload matches.
            existing = await self._existing_tx(user_id, idempotency_key)
            if existing is None:  # pragma: no cover - defensive
                raise ConflictError("idempotency conflict")
            if existing.type != "debit" or int(existing.amount) != amount:
                raise ConflictError("idempotency key reused with different payload")
            balance = await self._current_balance(user_id)
            return ConsumeResult(
                new_balance=balance, ledger_tx_id=existing.id, idempotent_replay=True
            )

        # New debit: conditional balance update (double guard against negative balance).
        updated = await self._session.scalar(
            text(
                "UPDATE wallets SET balance = balance - :amount, updated_at = now() "
                "WHERE user_id = :uid AND balance >= :amount "
                "RETURNING balance"
            ),
            {"uid": str(user_id), "amount": amount},
        )
        if updated is None:
            # Not enough credits: roll back the just-inserted ledger row by failing the tx.
            wallet_debit_total.labels(result="fail").inc()
            raise InsufficientCreditsError("insufficient_credits")

        new_balance = int(updated)
        wallet_debit_total.labels(result="success").inc()
        await self._audit.record(
            AuditEvent(
                user_id=user_id,
                session_id=session_id,
                event_type=EVENT_BILLING_DEBIT,
                payload={
                    "ledgerTxId": str(inserted_id),
                    "amount": amount,
                    "newBalance": new_balance,
                    "sessionId": str(session_id) if session_id else None,
                    "model": meta.get("model"),
                },
            )
        )
        return ConsumeResult(
            new_balance=new_balance, ledger_tx_id=inserted_id, idempotent_replay=False
        )

    async def grant(
        self,
        *,
        user_id: uuid.UUID,
        amount: int,
        idempotency_key: str,
        meta: dict[str, Any],
        reason: str,
    ) -> GrantResult:
        """Atomic, idempotent credit grant (ADR-006). amount > 0."""
        if amount <= 0:
            raise ConflictError("amount must be positive")
        await self._ensure_wallet(user_id)

        inserted_id = await self._session.scalar(
            text(
                "INSERT INTO ledger_transactions (user_id, type, amount, meta, idempotency_key) "
                "VALUES (:uid, 'credit', :amount, CAST(:meta AS JSONB), :key) "
                "ON CONFLICT (user_id, idempotency_key) DO NOTHING "
                "RETURNING id"
            ),
            {
                "uid": str(user_id),
                "amount": amount,
                "meta": _json(meta),
                "key": idempotency_key,
            },
        )

        if inserted_id is None:
            existing = await self._existing_tx(user_id, idempotency_key)
            if existing is None:  # pragma: no cover - defensive
                raise ConflictError("idempotency conflict")
            if existing.type != "credit" or int(existing.amount) != amount:
                raise ConflictError("idempotency key reused with different payload")
            balance = await self._current_balance(user_id)
            return GrantResult(
                new_balance=balance, ledger_tx_id=existing.id, idempotent_replay=True
            )

        updated = await self._session.scalar(
            text(
                "UPDATE wallets SET balance = balance + :amount, updated_at = now() "
                "WHERE user_id = :uid RETURNING balance"
            ),
            {"uid": str(user_id), "amount": amount},
        )
        new_balance = int(updated) if updated is not None else amount
        await self._audit.record(
            AuditEvent(
                user_id=user_id,
                event_type=EVENT_BILLING_CREDIT,
                payload={
                    "ledgerTxId": str(inserted_id),
                    "amount": amount,
                    "newBalance": new_balance,
                    "reason": reason,
                },
            )
        )
        return GrantResult(
            new_balance=new_balance, ledger_tx_id=inserted_id, idempotent_replay=False
        )

    async def get_wallet_view(
        self, user_id: uuid.UUID, last_n: int
    ) -> tuple[int, list[LedgerTransaction]]:
        await self._ensure_wallet(user_id)
        balance = await self._current_balance(user_id)
        txs = list(
            await self._session.scalars(
                select(LedgerTransaction)
                .where(LedgerTransaction.user_id == user_id)
                .order_by(LedgerTransaction.created_at.desc())
                .limit(last_n)
            )
        )
        return balance, txs


def _json(value: dict[str, Any]) -> str:
    import json

    return json.dumps(value)
