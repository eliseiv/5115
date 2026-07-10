"""Admin service (ADR-009, ADM-4/5/6/7): thin wrapper over WalletService.

Does NOT duplicate billing logic — it adds admin authorization context (caller already
passed ``require_admin``), a user-existence check (admin never creates users, ADR-007), an
extra ``admin_grant`` audit event, and the ``admin_grant_total`` metric. Idempotency,
ledger writes and the ``billing_credit`` audit stay in WalletService.grant.
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import (
    EVENT_ADMIN_GRANT,
    EVENT_ADMIN_SUBSCRIPTION_GRANT,
    AuditEvent,
    AuditService,
)
from app.config import get_settings
from app.errors import ConflictError, UserNotFoundError
from app.models import LedgerTransaction
from app.observability.metrics import admin_grant_total
from app.wallet.service import WalletService


@dataclass(frozen=True)
class AdminGrantResult:
    new_balance: int
    ledger_tx_id: uuid.UUID
    idempotent_replay: bool


@dataclass(frozen=True)
class AdminSubscriptionGrantResult:
    status: str
    expires_at: datetime.datetime
    plan: str
    credits_granted: int
    new_balance: int | None = None
    ledger_tx_id: uuid.UUID | None = None
    idempotent_replay: bool | None = None


@dataclass(frozen=True)
class AdminWalletView:
    user_id: uuid.UUID
    balance: int
    last_transactions: list[LedgerTransaction]


class AdminService:
    def __init__(self, session: AsyncSession, wallet: WalletService, audit: AuditService) -> None:
        self._session = session
        self._wallet = wallet
        self._audit = audit

    async def _require_user_exists(self, user_id: uuid.UUID) -> None:
        """Admin grant/view never creates users — missing userId is a 404 (ADR-009, ADR-007).

        Parameterized lookup. Done BEFORE WalletService.grant (which would _ensure_wallet but
        never create the users row) so an operator typo surfaces as user_not_found, not a silent
        phantom account.
        """
        exists = await self._session.scalar(
            text("SELECT 1 FROM users WHERE id = :uid"),
            {"uid": str(user_id)},
        )
        if exists is None:
            admin_grant_total.labels(result="not_found").inc()
            raise UserNotFoundError("user not found")

    async def grant(
        self,
        *,
        user_id: uuid.UUID,
        amount: int,
        idempotency_key: str,
        reason: str,
    ) -> AdminGrantResult:
        """Credit a user's wallet on behalf of an operator (saap/compensation).

        Reuses WalletService.grant verbatim (atomic, idempotent by (user_id, idempotency_key),
        writes ledger credit + billing_credit audit). Adds an admin_grant audit event recording
        the admin initiation (actor=admin, reason). The X-Admin-Token secret is never part of any
        payload. A reused key with a different amount surfaces as 409 (conflict).
        """
        await self._require_user_exists(user_id)
        meta: dict[str, Any] = {"source": "admin", "reason": reason}
        try:
            result = await self._wallet.grant(
                user_id=user_id,
                amount=amount,
                idempotency_key=idempotency_key,
                meta=meta,
                reason=reason,
            )
        except ConflictError:
            admin_grant_total.labels(result="conflict").inc()
            raise

        await self._audit.record(
            AuditEvent(
                user_id=user_id,
                event_type=EVENT_ADMIN_GRANT,
                payload={
                    "actor": "admin",
                    "userId": str(user_id),
                    "amount": amount,
                    "reason": reason,
                    "idempotencyKey": idempotency_key,
                    "ledgerTxId": str(result.ledger_tx_id),
                    "idempotentReplay": result.idempotent_replay,
                },
            )
        )
        admin_grant_total.labels(result="success").inc()
        return AdminGrantResult(
            new_balance=result.new_balance,
            ledger_tx_id=result.ledger_tx_id,
            idempotent_replay=result.idempotent_replay,
        )

    async def grant_subscription(
        self,
        *,
        user_id: uuid.UUID,
        expires_at: datetime.datetime,
        plan: str,
        idempotency_key: str,
        credits: int | None,
    ) -> AdminSubscriptionGrantResult:
        """Activate/extend a subscription for an operator, without a StoreKit transaction (ADR-048).

        Direct verify-less upsert of the ``subscriptions`` row (status='active', plan, expires_at)
        followed by an optional, idempotent credit grant in the SAME request transaction. Missing
        userId → 404 (admin never creates users, ADR-007). The effective credit amount defaults to
        SUBSCRIPTION_CREDITS_PER_PERIOD so an activated subscription behaves like a real period
        (ADR-048 §2); an explicit 0 activates without granting. A reused idempotencyKey with a
        different amount surfaces as 409 from WalletService.grant. The X-Admin-Token secret is
        never part of any audit payload or log.
        """
        await self._require_user_exists(user_id)
        settings = get_settings()
        effective_credits = (
            credits if credits is not None else settings.subscription_credits_per_period
        )

        # DB-level upsert (single statement) so a concurrent FIRST activation for a user with no
        # subscriptions row does not race two INSERTs into an IntegrityError — ON CONFLICT makes it
        # idempotent by PK (user_id). Parameterized; 'active' casts to the subscription-status enum.
        await self._session.execute(
            text(
                "INSERT INTO subscriptions (user_id, status, plan, expires_at, updated_at) "
                "VALUES (:uid, 'active', :plan, :expires_at, now()) "
                "ON CONFLICT (user_id) DO UPDATE SET "
                "status = 'active', plan = EXCLUDED.plan, "
                "expires_at = EXCLUDED.expires_at, updated_at = now()"
            ),
            {"uid": str(user_id), "plan": plan, "expires_at": expires_at},
        )
        await self._session.flush()

        new_balance: int | None = None
        ledger_tx_id: uuid.UUID | None = None
        idempotent_replay: bool | None = None
        if effective_credits > 0:
            # ConflictError (same key, different amount) propagates as 409; the shared request
            # transaction is not committed, so the subscription upsert does not persist either.
            grant = await self._wallet.grant(
                user_id=user_id,
                amount=effective_credits,
                idempotency_key=f"admin-sub-grant:{idempotency_key}",
                meta={"source": "admin", "reason": "admin_subscription_grant"},
                reason="admin_subscription_grant",
            )
            new_balance = grant.new_balance
            ledger_tx_id = grant.ledger_tx_id
            idempotent_replay = grant.idempotent_replay

        payload: dict[str, Any] = {
            "actor": "admin",
            "userId": str(user_id),
            "plan": plan,
            "status": "active",
            "expiresAt": expires_at.isoformat(),
            "creditsGranted": effective_credits,
            "idempotencyKey": idempotency_key,
        }
        if ledger_tx_id is not None:
            payload["ledgerTxId"] = str(ledger_tx_id)
        await self._audit.record(
            AuditEvent(
                user_id=user_id,
                event_type=EVENT_ADMIN_SUBSCRIPTION_GRANT,
                payload=payload,
            )
        )
        return AdminSubscriptionGrantResult(
            status="active",
            expires_at=expires_at,
            plan=plan,
            credits_granted=effective_credits,
            new_balance=new_balance,
            ledger_tx_id=ledger_tx_id,
            idempotent_replay=idempotent_replay,
        )

    async def get_wallet_view(self, user_id: uuid.UUID, last_n: int) -> AdminWalletView:
        """Read-only wallet view for support. Missing userId → 404 (never creates a user)."""
        await self._require_user_exists(user_id)
        balance, txs = await self._wallet.get_wallet_view(user_id, last_n)
        return AdminWalletView(user_id=user_id, balance=balance, last_transactions=txs)
