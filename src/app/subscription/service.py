"""Subscription service: verify → normalize → upsert → grant → audit (subscription/03)."""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import EVENT_SUBSCRIPTION_CHANGE, AuditEvent, AuditService
from app.config import get_settings
from app.models import Subscription
from app.subscription.storekit import StoreKitVerifier, VerifiedTransaction
from app.wallet.service import WalletService


@dataclass(frozen=True)
class SubscriptionResult:
    is_subscribed: bool
    expires_at: datetime.datetime | None
    plan: str | None


def _now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC)


class SubscriptionService:
    def __init__(
        self,
        session: AsyncSession,
        verifier: StoreKitVerifier,
        wallet: WalletService,
        audit: AuditService,
    ) -> None:
        self._session = session
        self._verifier = verifier
        self._wallet = wallet
        self._audit = audit

    async def sync(self, user_id: uuid.UUID, signed_transaction: str) -> SubscriptionResult:
        """Verify the StoreKit transaction and reconcile subscription + credit grant."""
        verified: VerifiedTransaction = self._verifier.verify(signed_transaction)

        active = (not verified.revoked) and (
            verified.expires_at is not None and verified.expires_at > _now()
        )
        status = "active" if active else "expired"

        row = await self._session.scalar(
            select(Subscription).where(Subscription.user_id == user_id)
        )
        if row is None:
            row = Subscription(
                user_id=user_id,
                status=status,
                plan=verified.product_id or None,
                expires_at=verified.expires_at,
            )
            self._session.add(row)
        else:
            row.status = status
            row.plan = verified.product_id or None
            row.expires_at = verified.expires_at
            row.updated_at = _now()
        await self._session.flush()

        # Grant a fixed credit package per period, idempotent by transactionId (ADR-006).
        if active:
            settings = get_settings()
            await self._wallet.grant(
                user_id=user_id,
                amount=settings.subscription_credits_per_period,
                idempotency_key=f"sub-grant:{verified.transaction_id}",
                meta={
                    "reason": "subscription_period",
                    "transactionId": verified.transaction_id,
                    "productId": verified.product_id,
                },
                reason="subscription_period",
            )

        await self._audit.record(
            AuditEvent(
                user_id=user_id,
                event_type=EVENT_SUBSCRIPTION_CHANGE,
                payload={
                    "status": status,
                    "plan": verified.product_id or None,
                    "transactionId": verified.transaction_id,
                    "environment": verified.environment,
                    "revoked": verified.revoked,
                },
            )
        )

        return SubscriptionResult(
            is_subscribed=active,
            expires_at=verified.expires_at,
            plan=verified.product_id or None,
        )
