"""Integration: subscription sync verify → grant → audit (AC-9, AC-10, ADR-006)."""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import AuditService
from app.errors import ValidationFailedError
from app.subscription.service import SubscriptionService
from app.subscription.storekit import VerifiedTransaction
from app.wallet.service import WalletService
from tests.conftest import FakeStoreKitVerifier, seed_user


def _svc(session: AsyncSession, verifier: FakeStoreKitVerifier) -> SubscriptionService:
    return SubscriptionService(
        session,
        verifier,
        WalletService(session, AuditService(session)),
        AuditService(session),  # type: ignore[arg-type]
    )


def _verified(tx_id: str, *, active: bool = True) -> VerifiedTransaction:
    expires = datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(days=30 if active else -1)
    return VerifiedTransaction(
        transaction_id=tx_id,
        original_transaction_id=tx_id,
        product_id="pro_monthly",
        expires_at=expires,
        revoked=False,
        environment="sandbox",
    )


@pytest.mark.asyncio
async def test_sync_active_grants_credits_and_audits(
    db_session: AsyncSession, fake_storekit: FakeStoreKitVerifier
) -> None:
    uid = await seed_user(db_session)
    fake_storekit.next_transaction = _verified("tx-1", active=True)

    res = await _svc(db_session, fake_storekit).sync(uid, "jws.token.here")
    await db_session.commit()
    assert res.is_subscribed is True

    bal = await db_session.scalar(
        text("SELECT balance FROM wallets WHERE user_id=:u"), {"u": str(uid)}
    )
    assert int(bal) == 1000  # SUBSCRIPTION_CREDITS_PER_PERIOD default

    sub_audit = await db_session.scalar(
        text(
            "SELECT count(*) FROM audit_logs WHERE user_id=:u "
            "AND event_type='subscription_change'"
        ),
        {"u": str(uid)},
    )
    credit_audit = await db_session.scalar(
        text("SELECT count(*) FROM audit_logs WHERE user_id=:u AND event_type='billing_credit'"),
        {"u": str(uid)},
    )
    assert int(sub_audit) == 1
    assert int(credit_audit) == 1


@pytest.mark.asyncio
async def test_resync_same_transaction_does_not_double_grant(
    db_session: AsyncSession, fake_storekit: FakeStoreKitVerifier
) -> None:
    uid = await seed_user(db_session)
    fake_storekit.next_transaction = _verified("tx-dup", active=True)
    await _svc(db_session, fake_storekit).sync(uid, "jws")
    await db_session.commit()
    await _svc(db_session, fake_storekit).sync(uid, "jws")
    await db_session.commit()
    bal = await db_session.scalar(
        text("SELECT balance FROM wallets WHERE user_id=:u"), {"u": str(uid)}
    )
    assert int(bal) == 1000  # granted exactly once (idempotent by transactionId)


@pytest.mark.asyncio
async def test_forged_transaction_raises_validation_error(
    db_session: AsyncSession, fake_storekit: FakeStoreKitVerifier
) -> None:
    uid = await seed_user(db_session)
    fake_storekit.raise_error = True
    with pytest.raises(ValidationFailedError):
        await _svc(db_session, fake_storekit).sync(uid, "forged.jws.x")


@pytest.mark.asyncio
async def test_expired_transaction_marks_expired_no_grant(
    db_session: AsyncSession, fake_storekit: FakeStoreKitVerifier
) -> None:
    uid = await seed_user(db_session)
    fake_storekit.next_transaction = _verified("tx-exp", active=False)
    res = await _svc(db_session, fake_storekit).sync(uid, "jws")
    await db_session.commit()
    assert res.is_subscribed is False
    bal = await db_session.scalar(
        text("SELECT balance FROM wallets WHERE user_id=:u"), {"u": str(uid)}
    )
    assert bal is None or int(bal) == 0
