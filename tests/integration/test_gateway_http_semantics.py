"""Integration: HTTP semantics at the gateway (AC-10).

401 (missing/broken JWT), 403 (userId != sub), 413 (size), 422 (validation / forged
StoreKit), business-blocked → 200 with blockReason.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import FakeStoreKitVerifier, auth_headers, seed_user


@pytest.mark.asyncio
async def test_missing_token_401(client: AsyncClient) -> None:
    r = await client.post(
        "/v1/chat/run",
        json={
            "userId": str(uuid.uuid4()),
            "projectId": "p",
            "message": "hi",
            "mode": "credits",
        },
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_broken_jwt_401(client: AsyncClient) -> None:
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uuid.uuid4()), "projectId": "p", "message": "hi", "mode": "credits"},
        headers={"Authorization": "Bearer not.a.jwt"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_expired_jwt_401(client: AsyncClient) -> None:
    uid = uuid.uuid4()
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "projectId": "p", "message": "hi", "mode": "credits"},
        headers=auth_headers(uid, expired=True),
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_userid_mismatch_403(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, trial_used=False)
    other = uuid.uuid4()
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(other), "projectId": "p", "message": "hi", "mode": "credits"},
        headers=auth_headers(uid),  # token sub = uid, body userId = other
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_body_validation_422(client: AsyncClient) -> None:
    uid = uuid.uuid4()
    # empty message violates min_length=1
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "projectId": "p", "message": "", "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_oversized_body_413(client: AsyncClient) -> None:
    # ADR-020: the transport size limit is now PER-ROUTE. The general ≤512KB cap still applies to
    # ordinary routes (here /v1/wallet) — an oversized body is rejected at the middleware with 413
    # before parsing. /v1/chat/run has its OWN raised limit (12MB for inline base64 attachments)
    # and is covered separately in test_chat_attachments.py; it must NOT be 413 at 600KB.
    big = b"x" * (600 * 1024)  # > size_limit_body (512KiB)
    uid = uuid.uuid4()
    r = await client.post(
        "/v1/wallet/me",
        content=big,
        headers={**auth_headers(uid), "content-type": "application/json"},
    )
    assert r.status_code == 413


@pytest.mark.asyncio
async def test_forged_storekit_422(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_storekit: FakeStoreKitVerifier,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    fake_storekit.raise_error = True
    r = await client.post(
        "/v1/subscription/sync",
        json={"userId": str(uid), "transaction": "forged.jws.value"},
        headers=auth_headers(uid),
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_business_blocked_returns_200(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, trial_used=True)  # trial used, no subscription → blocked
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "projectId": "p", "message": "hi", "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "blocked"
    assert body["blockReason"] == "trial_used"


@pytest.mark.asyncio
async def test_security_headers_present(client: AsyncClient) -> None:
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert "X-Request-Id" in r.headers
