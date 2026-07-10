"""Integration tests for Wallet/Ledger atomicity + idempotency (AC-3, AC-4, ADR-005/006).

Real PostgreSQL (testcontainers). External clients not involved.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.service import AuditService
from app.errors import ConflictError, InsufficientCreditsError
from app.wallet.service import WalletService
from tests.conftest import seed_user


def _svc(session: AsyncSession) -> WalletService:
    return WalletService(session, AuditService(session))


async def _balance(session: AsyncSession, uid: uuid.UUID) -> int:
    row = await session.scalar(
        text("SELECT balance FROM wallets WHERE user_id = :u"), {"u": str(uid)}
    )
    return int(row) if row is not None else 0


async def _debit_count(session: AsyncSession, uid: uuid.UUID) -> int:
    row = await session.scalar(
        text("SELECT count(*) FROM ledger_transactions WHERE user_id = :u AND type='debit'"),
        {"u": str(uid)},
    )
    return int(row)


@pytest.mark.asyncio
async def test_consume_idempotent_same_key(db_session: AsyncSession) -> None:
    uid = await seed_user(db_session, balance=5)
    key = str(uuid.uuid4())

    r1 = await _svc(db_session).consume(
        user_id=uid, amount=1, idempotency_key=key, meta={"model": "m"}
    )
    await db_session.commit()
    assert r1.new_balance == 4
    assert r1.idempotent_replay is False

    r2 = await _svc(db_session).consume(
        user_id=uid, amount=1, idempotency_key=key, meta={"model": "m"}
    )
    await db_session.commit()
    assert r2.idempotent_replay is True
    assert await _balance(db_session, uid) == 4  # charged once
    assert await _debit_count(db_session, uid) == 1


@pytest.mark.asyncio
async def test_consume_rejects_negative_balance(db_session: AsyncSession) -> None:
    uid = await seed_user(db_session, balance=0)
    with pytest.raises(InsufficientCreditsError):
        await _svc(db_session).consume(
            user_id=uid, amount=1, idempotency_key=str(uuid.uuid4()), meta={}
        )
    await db_session.rollback()
    assert await _balance(db_session, uid) == 0


@pytest.mark.asyncio
async def test_consume_same_key_different_amount_conflicts(db_session: AsyncSession) -> None:
    uid = await seed_user(db_session, balance=10)
    key = str(uuid.uuid4())
    await _svc(db_session).consume(user_id=uid, amount=1, idempotency_key=key, meta={})
    await db_session.commit()
    with pytest.raises(ConflictError, match="different payload"):
        await _svc(db_session).consume(user_id=uid, amount=2, idempotency_key=key, meta={})


@pytest.mark.asyncio
async def test_concurrent_consume_distinct_keys_charges_each(
    db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
) -> None:
    """N parallel debits with distinct keys → exactly N charges, balance never negative."""
    uid = await seed_user(db_session, balance=10)

    async def one(key: str) -> None:
        async with db_sessionmaker() as s:
            try:
                await _svc(s).consume(user_id=uid, amount=1, idempotency_key=key, meta={})
                await s.commit()
            except InsufficientCreditsError:
                await s.rollback()

    keys = [str(uuid.uuid4()) for _ in range(10)]
    await asyncio.gather(*(one(k) for k in keys))

    async with db_sessionmaker() as s:
        assert await _balance(s, uid) == 0
        assert await _debit_count(s, uid) == 10


@pytest.mark.asyncio
async def test_concurrent_consume_same_key_charges_once(
    db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
) -> None:
    """Same idempotency key from many parallel callers → exactly one debit (AC-3)."""
    uid = await seed_user(db_session, balance=10)
    key = str(uuid.uuid4())

    async def one() -> None:
        async with db_sessionmaker() as s:
            try:
                await _svc(s).consume(user_id=uid, amount=1, idempotency_key=key, meta={})
                await s.commit()
            except ConflictError:
                await s.rollback()

    await asyncio.gather(*(one() for _ in range(8)))

    async with db_sessionmaker() as s:
        assert await _balance(s, uid) == 9  # charged exactly once
        assert await _debit_count(s, uid) == 1


@pytest.mark.asyncio
async def test_concurrent_overspend_never_negative(
    db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
) -> None:
    """More distinct debits than balance → balance stays 0, exactly `balance` succeed."""
    uid = await seed_user(db_session, balance=3)
    successes = 0
    lock = asyncio.Lock()

    async def one(key: str) -> None:
        nonlocal successes
        async with db_sessionmaker() as s:
            try:
                await _svc(s).consume(user_id=uid, amount=1, idempotency_key=key, meta={})
                await s.commit()
                async with lock:
                    successes += 1
            except InsufficientCreditsError:
                await s.rollback()

    await asyncio.gather(*(one(str(uuid.uuid4())) for _ in range(10)))
    async with db_sessionmaker() as s:
        assert await _balance(s, uid) == 0
    assert successes == 3


@pytest.mark.asyncio
async def test_grant_idempotent_by_transaction(db_session: AsyncSession) -> None:
    uid = await seed_user(db_session, balance=0)
    key = "sub-grant:tx-123"
    g1 = await _svc(db_session).grant(
        user_id=uid, amount=1000, idempotency_key=key, meta={}, reason="subscription_period"
    )
    await db_session.commit()
    assert g1.new_balance == 1000

    g2 = await _svc(db_session).grant(
        user_id=uid, amount=1000, idempotency_key=key, meta={}, reason="subscription_period"
    )
    await db_session.commit()
    assert g2.idempotent_replay is True
    assert await _balance(db_session, uid) == 1000  # granted once


@pytest.mark.asyncio
async def test_consume_rejects_nonpositive_amount(db_session: AsyncSession) -> None:
    uid = await seed_user(db_session, balance=5)
    with pytest.raises(ConflictError):
        await _svc(db_session).consume(user_id=uid, amount=0, idempotency_key="k", meta={})


@pytest.mark.asyncio
async def test_debit_writes_audit(db_session: AsyncSession) -> None:
    uid = await seed_user(db_session, balance=5)
    await _svc(db_session).consume(
        user_id=uid, amount=1, idempotency_key=str(uuid.uuid4()), meta={"model": "m"}
    )
    await db_session.commit()
    n = await db_session.scalar(
        text("SELECT count(*) FROM audit_logs WHERE user_id=:u AND event_type='billing_debit'"),
        {"u": str(uid)},
    )
    assert int(n) == 1
