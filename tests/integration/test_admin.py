"""Integration: admin /v1/admin/* (ADR-009, admin/09-testing.md). Real PostgreSQL.

X-Admin-Token authorization (isolated from JWT), idempotent grant, 404 for missing user,
audit (admin_grant + billing_credit, no secret), rate-limit, size-limit, security isolation.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from tests.conftest import FakeAnthropicClient, FakeStoreKitVerifier, auth_headers, seed_user

_ADMIN_SECRET = "admin-secret-integration-0123456789abcdef0123456789"
_ADMIN_PREV = "admin-secret-prev-integration-0123456789abcdef0123"
_ADMIN_HEADERS = {"X-Admin-Token": _ADMIN_SECRET}


@pytest.fixture
async def admin_client(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    fake_storekit: FakeStoreKitVerifier,
) -> AsyncIterator[AsyncClient]:
    """Shared `client` analogue with admin secrets configured and admin rate-limit forced open."""
    settings = get_settings()
    orig_secret, orig_prev = settings.admin_api_secret, settings.admin_api_secret_prev
    settings.admin_api_secret = _ADMIN_SECRET
    settings.admin_api_secret_prev = _ADMIN_PREV

    from app import deps
    from app.api_gateway import rate_limit
    from app.api_gateway.routers import admin as admin_router
    from app.chat import anthropic_client as anthropic_mod
    from app.main import create_app
    from app.subscription import storekit as storekit_mod

    async def _override_db() -> AsyncIterator[AsyncSession]:
        async with db_sessionmaker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    anthropic_mod._anthropic_singleton = fake_anthropic  # type: ignore[assignment]
    storekit_mod._verifier_singleton = fake_storekit  # type: ignore[assignment]

    async def _allow_admin(**_kwargs: Any) -> bool:
        return True

    orig_admin = rate_limit.enforce_admin_limits
    rate_limit.enforce_admin_limits = _allow_admin  # type: ignore[assignment]
    admin_router.enforce_admin_limits = _allow_admin  # type: ignore[assignment]

    app = create_app()
    app.dependency_overrides[deps.get_db] = _override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    settings.admin_api_secret = orig_secret
    settings.admin_api_secret_prev = orig_prev
    rate_limit.enforce_admin_limits = orig_admin  # type: ignore[assignment]
    admin_router.enforce_admin_limits = orig_admin  # type: ignore[assignment]


async def _balance(maker: async_sessionmaker[AsyncSession], uid: str) -> int:
    async with maker() as s:
        row = await s.scalar(text("SELECT balance FROM wallets WHERE user_id=:u"), {"u": uid})
        return int(row) if row is not None else 0


async def _count(maker: async_sessionmaker[AsyncSession], sql: str, uid: str) -> int:
    async with maker() as s:
        return int(await s.scalar(text(sql), {"u": uid}) or 0)


# --------------------------- grant: success + ledger + balance + audit ---------------------------
@pytest.mark.asyncio
async def test_grant_success_increases_balance_and_audits(
    admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, balance=0)
    r = await admin_client.post(
        "/v1/admin/wallet/grant",
        json={"userId": str(uid), "amount": 50, "idempotencyKey": "g-1", "reason": "support"},
        headers=_ADMIN_HEADERS,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["newBalance"] == 50
    assert body["idempotentReplay"] is False
    assert await _balance(db_sessionmaker, str(uid)) == 50

    credits = await _count(
        db_sessionmaker,
        "SELECT count(*) FROM ledger_transactions WHERE user_id=:u AND type='credit'",
        str(uid),
    )
    assert credits == 1
    admin_grant = await _count(
        db_sessionmaker,
        "SELECT count(*) FROM audit_logs WHERE user_id=:u AND event_type='admin_grant'",
        str(uid),
    )
    billing_credit = await _count(
        db_sessionmaker,
        "SELECT count(*) FROM audit_logs WHERE user_id=:u AND event_type='billing_credit'",
        str(uid),
    )
    assert admin_grant == 1
    assert billing_credit == 1

    # No admin secret leaked into any audit payload.
    async with db_sessionmaker() as s:
        rows = await s.scalars(
            text("SELECT payload::text FROM audit_logs WHERE user_id=:u"), {"u": str(uid)}
        )
        blob = " ".join(rows)
    assert _ADMIN_SECRET not in blob
    assert _ADMIN_PREV not in blob


# --------------------------- grant: idempotency ---------------------------
@pytest.mark.asyncio
async def test_grant_idempotent_replay_no_double_credit(
    admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, balance=0)
    payload = {"userId": str(uid), "amount": 25, "idempotencyKey": "g-dup", "reason": "x"}
    r1 = await admin_client.post("/v1/admin/wallet/grant", json=payload, headers=_ADMIN_HEADERS)
    r2 = await admin_client.post("/v1/admin/wallet/grant", json=payload, headers=_ADMIN_HEADERS)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["idempotentReplay"] is False
    assert r2.json()["idempotentReplay"] is True
    assert r1.json()["ledgerTxId"] == r2.json()["ledgerTxId"]
    assert await _balance(db_sessionmaker, str(uid)) == 25  # credited once
    credits = await _count(
        db_sessionmaker,
        "SELECT count(*) FROM ledger_transactions WHERE user_id=:u AND type='credit'",
        str(uid),
    )
    assert credits == 1


@pytest.mark.asyncio
async def test_grant_same_key_different_amount_409(
    admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, balance=0)
    await admin_client.post(
        "/v1/admin/wallet/grant",
        json={"userId": str(uid), "amount": 10, "idempotencyKey": "g-c", "reason": "x"},
        headers=_ADMIN_HEADERS,
    )
    r = await admin_client.post(
        "/v1/admin/wallet/grant",
        json={"userId": str(uid), "amount": 99, "idempotencyKey": "g-c", "reason": "x"},
        headers=_ADMIN_HEADERS,
    )
    assert r.status_code == 409
    assert await _balance(db_sessionmaker, str(uid)) == 10  # second did not credit


# --------------------------- grant: 404 / validation ---------------------------
@pytest.mark.asyncio
async def test_grant_unknown_user_404_no_user_created(
    admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    missing = uuid.uuid4()
    r = await admin_client.post(
        "/v1/admin/wallet/grant",
        json={"userId": str(missing), "amount": 5, "idempotencyKey": "g-x", "reason": "x"},
        headers=_ADMIN_HEADERS,
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "user_not_found"
    # No phantom user / wallet created.
    async with db_sessionmaker() as s:
        exists = await s.scalar(text("SELECT count(*) FROM users WHERE id=:u"), {"u": str(missing)})
        wallet = await s.scalar(
            text("SELECT count(*) FROM wallets WHERE user_id=:u"), {"u": str(missing)}
        )
    assert int(exists) == 0
    assert int(wallet) == 0


@pytest.mark.asyncio
async def test_grant_missing_reason_422(admin_client: AsyncClient) -> None:
    r = await admin_client.post(
        "/v1/admin/wallet/grant",
        json={"userId": str(uuid.uuid4()), "amount": 5, "idempotencyKey": "g"},
        headers=_ADMIN_HEADERS,
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_grant_nonpositive_amount_422(admin_client: AsyncClient) -> None:
    r = await admin_client.post(
        "/v1/admin/wallet/grant",
        json={"userId": str(uuid.uuid4()), "amount": 0, "idempotencyKey": "g", "reason": "x"},
        headers=_ADMIN_HEADERS,
    )
    assert r.status_code == 422


# --------------------------- wallet view ---------------------------
@pytest.mark.asyncio
async def test_wallet_view_returns_balance_and_ledger(
    admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, balance=0)
    await admin_client.post(
        "/v1/admin/wallet/grant",
        json={"userId": str(uid), "amount": 30, "idempotencyKey": "v-1", "reason": "x"},
        headers=_ADMIN_HEADERS,
    )
    r = await admin_client.get(f"/v1/admin/wallet/{uid}", headers=_ADMIN_HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["userId"] == str(uid)
    assert body["balance"] == 30
    assert len(body["lastTransactions"]) == 1
    assert body["lastTransactions"][0]["type"] == "credit"


@pytest.mark.asyncio
async def test_wallet_view_unknown_user_404(admin_client: AsyncClient) -> None:
    r = await admin_client.get(f"/v1/admin/wallet/{uuid.uuid4()}", headers=_ADMIN_HEADERS)
    assert r.status_code == 404


# --------------------------- security / authorization ---------------------------
@pytest.mark.asyncio
async def test_no_admin_token_401(admin_client: AsyncClient) -> None:
    r = await admin_client.post(
        "/v1/admin/wallet/grant",
        json={"userId": str(uuid.uuid4()), "amount": 5, "idempotencyKey": "g", "reason": "x"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_wrong_admin_token_401(admin_client: AsyncClient) -> None:
    r = await admin_client.get(
        f"/v1/admin/wallet/{uuid.uuid4()}", headers={"X-Admin-Token": "nope"}
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_prev_admin_token_accepted_during_rotation(
    admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, balance=0)
    r = await admin_client.post(
        "/v1/admin/wallet/grant",
        json={"userId": str(uid), "amount": 7, "idempotencyKey": "rot", "reason": "x"},
        headers={"X-Admin-Token": _ADMIN_PREV},
    )
    assert r.status_code == 200
    assert r.json()["newBalance"] == 7


@pytest.mark.asyncio
async def test_user_jwt_does_not_authorize_admin_route(
    admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, balance=0)
    # A perfectly valid user JWT (but no X-Admin-Token) must NOT authorize /v1/admin/*.
    r = await admin_client.post(
        "/v1/admin/wallet/grant",
        json={"userId": str(uid), "amount": 5, "idempotencyKey": "jwt", "reason": "x"},
        headers=auth_headers(uid),
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_admin_token_does_not_authorize_user_route(admin_client: AsyncClient) -> None:
    # X-Admin-Token on a user route (/v1/wallet) gives no access — that route needs a JWT.
    r = await admin_client.get("/v1/wallet", headers=_ADMIN_HEADERS)
    assert r.status_code == 401


# --------------------------- size limit ---------------------------
@pytest.mark.asyncio
async def test_admin_body_over_8kb_413(
    admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # A schema-valid body whose raw bytes exceed the 8 KB admin cap (inflated with JSON
    # whitespace) must be rejected with 413 by _enforce_admin_body_size (ADR-009 §6), stricter
    # than the global 512 KB SizeLimitMiddleware.
    async with db_sessionmaker() as s:
        uid = await seed_user(s, balance=0)
    padding = " " * (9 * 1024)  # whitespace keeps the JSON schema-valid but > 8 KB on the wire
    raw = f'{{"userId": "{uid}", "amount": 5, "idempotencyKey": "big", "reason": "x"{padding}}}'
    r = await admin_client.post(
        "/v1/admin/wallet/grant",
        content=raw.encode(),
        headers={**_ADMIN_HEADERS, "Content-Type": "application/json"},
    )
    assert r.status_code == 413, r.text


@pytest.mark.asyncio
async def test_admin_field_over_schema_cap_422(
    admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # A reason longer than the schema cap (512) is a validation error (422), independent of size.
    async with db_sessionmaker() as s:
        uid = await seed_user(s, balance=0)
    r = await admin_client.post(
        "/v1/admin/wallet/grant",
        json={"userId": str(uid), "amount": 5, "idempotencyKey": "big", "reason": "x" * 600},
        headers=_ADMIN_HEADERS,
    )
    assert r.status_code == 422


# --------------------------- rate limit (limiter-level isolation) ---------------------------
@pytest.mark.asyncio
async def test_admin_rate_limit_returns_429(
    admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, balance=0)

    # Force the admin limiter to deny (simulates exceeding ADMIN_RATE_LIMIT_PER_MIN).
    from app.api_gateway.routers import admin as admin_router

    async def _deny(**_kwargs: Any) -> bool:
        return False

    prev = admin_router.enforce_admin_limits
    admin_router.enforce_admin_limits = _deny  # type: ignore[assignment]
    try:
        r = await admin_client.post(
            "/v1/admin/wallet/grant",
            json={"userId": str(uid), "amount": 5, "idempotencyKey": "rl", "reason": "x"},
            headers=_ADMIN_HEADERS,
        )
        assert r.status_code == 429
    finally:
        admin_router.enforce_admin_limits = prev  # type: ignore[assignment]
