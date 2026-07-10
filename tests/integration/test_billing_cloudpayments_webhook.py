"""Integration: ADR-054 — RU CloudPayments webhook END-TO-END over HTTP against real PostgreSQL.

Drives the FULL path ``POST /v1/billing/cloudpayments/webhook`` through the observational (non-
blocking) auth dependency, the per-source-IP rate limit and the verification service. ADR-054 makes
the endpoint PUBLIC (broadapps sends no auth), so there is NO 401; the trust anchor is the broadapps
API verification, which is FAKED here (``deps.get_cloudpayments_verify_client`` is overridden with a
scripted client) so there is NO network. Hermetic: the DB is the shared testcontainers Postgres
(seeded via ``seed_user``); CLOUDPAYMENTS_* + TOKEN_PRODUCTS via env + ``get_settings`` cache-clear
(restored); no LLM.

Covers §1 publicity (no 401 with/without Authorization), the config-activation gate
(``CLOUDPAYMENTS_API_TOKEN=""`` -> 500 misconfigured), the per-IP rate limit (429), the happy path
(fresh ``succeeded`` -> 200 ``{"code":0}`` + credit), ``user_not_found`` -> 200, and a transient
verify failure -> 500 retriable.
"""

from __future__ import annotations

import datetime
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.errors import CloudPaymentsVerificationUnavailableError
from tests.conftest import seed_user

_URL = "/v1/billing/cloudpayments/webhook"
_TOKEN_CODE = "100_tokens_9.99"
_TOKEN_CREDITS = 100
_UID_UPPER = "B284721F-C3E0-4446-B00F-3C6A21F32535"


class FakeVerifyClient:
    def __init__(
        self, *, payments: list[dict[str, Any]] | None = None, error: Exception | None = None
    ) -> None:
        self._payments = payments if payments is not None else []
        self._error = error
        self.calls: list[uuid.UUID] = []

    def set_payments(self, payments: list[dict[str, Any]]) -> None:
        self._payments = payments
        self._error = None

    def set_error(self, error: Exception) -> None:
        self._error = error

    async def list_payments(self, *, device_id: uuid.UUID) -> list[dict[str, Any]]:
        self.calls.append(device_id)
        if self._error is not None:
            raise self._error
        return [dict(p) for p in self._payments]


def _payment(payment_id: str = "pay-1") -> dict[str, Any]:
    return {
        "payment_id": payment_id,
        "status": "succeeded",
        "paid_at": (
            datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(minutes=5)
        ).isoformat(),
        "product": {"code": _TOKEN_CODE, "payment_type": "one_time"},
    }


@pytest.fixture
async def fake_verify() -> FakeVerifyClient:
    return FakeVerifyClient(payments=[_payment()])


@pytest.fixture
async def cp_client(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_verify: FakeVerifyClient,
) -> AsyncIterator[AsyncClient]:
    """ASGI client wired to the container DB with CLOUDPAYMENTS_* set and verify client faked."""
    from app import deps
    from app.main import create_app

    monkeypatch.setenv("CLOUDPAYMENTS_API_TOKEN", "verify-token-secret")  # config gate: active
    monkeypatch.setenv("TOKEN_PRODUCTS", json.dumps({_TOKEN_CODE: _TOKEN_CREDITS}))
    get_settings.cache_clear()

    # Override the verify-client factory used by get_cloudpayments_webhook_service (bare-name call
    # in deps.py) -> no network to broadapps.
    monkeypatch.setattr(deps, "get_cloudpayments_verify_client", lambda: fake_verify)

    async def _override_db() -> AsyncIterator[AsyncSession]:
        async with db_sessionmaker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app = create_app()
    app.dependency_overrides[deps.get_db] = _override_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    get_settings.cache_clear()


def _body(*, account_id: str | None = _UID_UPPER, status: str = "Completed") -> bytes:
    body: dict[str, Any] = {
        "Status": status,
        "OperationType": "Payment",
        "Amount": 3990,
        "Currency": "RUB",
    }
    if account_id is not None:
        body["AccountId"] = account_id
    return json.dumps(body).encode()


async def _balance(maker: async_sessionmaker[AsyncSession], uid: uuid.UUID) -> int | None:
    async with maker() as s:
        bal = await s.scalar(text("SELECT balance FROM wallets WHERE user_id=:u"), {"u": str(uid)})
        return None if bal is None else int(bal)


# ============================ §1 Publicity — NO 401 ============================


@pytest.mark.asyncio
async def test_no_authorization_is_not_401(
    cp_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    uid = uuid.UUID(_UID_UPPER.lower())
    async with db_sessionmaker() as s:
        await seed_user(s, user_id=uid)
    # Public: a callback WITHOUT Authorization reaches the handler and credits (NOT 401).
    r = await cp_client.post(_URL, content=_body())
    assert r.status_code == 200, r.text
    assert r.json() == {"code": 0}
    assert await _balance(db_sessionmaker, uid) == _TOKEN_CREDITS


@pytest.mark.asyncio
@pytest.mark.parametrize("header", ["Bearer whatever", "garbage", "Basic x"])
async def test_any_authorization_is_accepted_not_401(
    cp_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], header: str
) -> None:
    uid = uuid.UUID(_UID_UPPER.lower())
    async with db_sessionmaker() as s:
        await seed_user(s, user_id=uid)
    r = await cp_client.post(_URL, content=_body(), headers={"Authorization": header})
    assert r.status_code == 200, r.text
    assert r.json() == {"code": 0}


# ============================ §1 Config-activation gate — 500 ============================


@pytest.mark.asyncio
async def test_unset_api_token_is_500_misconfigured(
    cp_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Without CLOUDPAYMENTS_API_TOKEN verification is impossible -> 500 (aggregator retries).
    monkeypatch.setenv("CLOUDPAYMENTS_API_TOKEN", "")
    get_settings.cache_clear()
    r = await cp_client.post(_URL, content=_body())
    assert r.status_code == 500, r.text


# ============================ §1 Rate limit — 429 ============================


@pytest.mark.asyncio
async def test_per_ip_flood_is_429(cp_client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api_gateway.routers import billing_cloudpayments as router_mod

    async def _deny(**_kwargs: Any) -> bool:
        return False

    # Force the per-source-IP limiter to reject (Redis-backed limiter fails open without Redis).
    monkeypatch.setattr(router_mod, "enforce_cloudpayments_webhook_limits", _deny)
    r = await cp_client.post(_URL, content=_body())
    assert r.status_code == 429, r.text


# ============================ Happy path / outcomes over HTTP ============================


@pytest.mark.asyncio
async def test_verified_succeeded_payment_credits_and_returns_ack(
    cp_client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_verify: FakeVerifyClient,
) -> None:
    uid = uuid.UUID(_UID_UPPER.lower())
    async with db_sessionmaker() as s:
        await seed_user(s, user_id=uid)
    r = await cp_client.post(_URL, content=_body())
    assert r.status_code == 200 and r.json() == {"code": 0}
    assert fake_verify.calls == [uid]
    assert await _balance(db_sessionmaker, uid) == _TOKEN_CREDITS


@pytest.mark.asyncio
async def test_user_not_found_is_200_no_credit(
    cp_client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_verify: FakeVerifyClient,
) -> None:
    # A valid UUID with no users row: 200 {"code":0}, verify NOT called, no grant.
    uid = uuid.uuid4()
    r = await cp_client.post(_URL, content=_body(account_id=str(uid).upper()))
    assert r.status_code == 200 and r.json() == {"code": 0}
    assert fake_verify.calls == []
    assert await _balance(db_sessionmaker, uid) is None


@pytest.mark.asyncio
async def test_transient_verify_failure_is_500(
    cp_client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_verify: FakeVerifyClient,
) -> None:
    uid = uuid.UUID(_UID_UPPER.lower())
    async with db_sessionmaker() as s:
        await seed_user(s, user_id=uid)
    fake_verify.set_error(CloudPaymentsVerificationUnavailableError("unavailable"))
    r = await cp_client.post(_URL, content=_body())
    assert r.status_code == 500, r.text
    assert await _balance(db_sessionmaker, uid) is None


@pytest.mark.asyncio
async def test_gate_non_completed_is_200_no_credit(
    cp_client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_verify: FakeVerifyClient,
) -> None:
    uid = uuid.UUID(_UID_UPPER.lower())
    async with db_sessionmaker() as s:
        await seed_user(s, user_id=uid)
    r = await cp_client.post(_URL, content=_body(status="Pending"))
    assert r.status_code == 200 and r.json() == {"code": 0}
    assert fake_verify.calls == []  # gate fails before verify
    assert await _balance(db_sessionmaker, uid) is None
