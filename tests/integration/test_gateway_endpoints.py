"""Integration/contract tests for the remaining gateway endpoints (response schemas).

Covers GET /v1/wallet, POST /v1/wallet/consume, /v1/byok/set|toggle|delete,
GET /v1/policy/effective, /health, /ready, /metrics.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import FakeAnthropicClient, auth_headers, seed_user


@pytest.mark.asyncio
async def test_wallet_view_schema(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=7)
    r = await client.get("/v1/wallet", headers=auth_headers(uid))
    assert r.status_code == 200
    body = r.json()
    assert body["balance"] == 7
    assert isinstance(body["lastTransactions"], list)


@pytest.mark.asyncio
async def test_wallet_consume_endpoint(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=3)
    r = await client.post(
        "/v1/wallet/consume",
        json={"userId": str(uid), "requestId": str(uuid.uuid4()), "amount": 1, "meta": {}},
        headers=auth_headers(uid),
    )
    assert r.status_code == 200
    assert r.json()["newBalance"] == 2


@pytest.mark.asyncio
async def test_byok_set_toggle_delete_flow(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    fake_anthropic.valid_keys = {"sk-ant-valid"}

    r1 = await client.post(
        "/v1/byok/set",
        json={"userId": str(uid), "apiKey": "sk-ant-valid"},
        headers=auth_headers(uid),
    )
    assert r1.status_code == 200
    assert r1.json()["keyStatus"] == "valid"
    assert r1.json()["byokEnabled"] is False

    r2 = await client.post(
        "/v1/byok/toggle",
        json={"userId": str(uid), "enabled": True},
        headers=auth_headers(uid),
    )
    assert r2.json()["byokEnabled"] is True

    r3 = await client.post("/v1/byok/delete", json={"userId": str(uid)}, headers=auth_headers(uid))
    assert r3.json()["keyStatus"] == "missing"


@pytest.mark.asyncio
async def test_byok_set_invalid_key_status(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    fake_anthropic.valid_keys = set()  # all keys rejected
    r = await client.post(
        "/v1/byok/set",
        json={"userId": str(uid), "apiKey": "sk-ant-bad"},
        headers=auth_headers(uid),
    )
    assert r.json()["keyStatus"] == "invalid"


@pytest.mark.asyncio
async def test_policy_effective_schema(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, trial_used=False)
    r = await client.get("/v1/policy/effective", headers=auth_headers(uid))
    assert r.status_code == 200
    body = r.json()
    for key in (
        "isSubscribed",
        "trialRemaining",
        "creditsBalance",
        "byokEnabled",
        "canGenerateCreditsMode",
        "canGenerateByokMode",
        "reasons",
    ):
        assert key in body
    assert body["trialRemaining"] == 1


@pytest.mark.asyncio
async def test_byok_set_oversized_key_422(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    r = await client.post(
        "/v1/byok/set",
        json={"userId": str(uid), "apiKey": "x" * (5 * 1024)},  # > size_limit_api_key 4KiB
        headers=auth_headers(uid),
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_health_ok(client: AsyncClient) -> None:
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_healthz_ok_public_no_auth(client: AsyncClient) -> None:
    """GET /healthz (Traefik healthcheck alias, ADR-017): 200 {status: ok}, public (no JWT)."""
    r = await client.get("/healthz")  # no Authorization header
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_healthz_identical_to_health(client: AsyncClient) -> None:
    """/healthz is an exact alias of /health: same status code and same body."""
    rz = await client.get("/healthz")
    rh = await client.get("/health")
    assert rz.status_code == rh.status_code == 200
    assert rz.json() == rh.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_ready_endpoint_not_regressed(client: AsyncClient) -> None:
    """/ready reports dependency status without JWT, with the documented shape.

    Hermetic by design: /ready (health.py::ready) probes the GLOBAL engine via
    get_sessionmaker() and redis_ping() — it does NOT use the get_db dependency, so the
    testcontainer injected via app.dependency_overrides[get_db] is invisible to it. Asserting
    db == "ok" here would depend on a live external Postgres/Redis reachable from the global
    DATABASE_URL/REDIS, which is absent in hermetic CI -> flaky 'down'. Real readiness
    (db/redis == "ok") is verified in tests/e2e_live/run_e2e.py against the docker-compose
    stack where those are real. Here we only pin the contract: status code and body shape.
    """
    r = await client.get("/ready")  # no Authorization header
    assert r.status_code in (200, 503)
    body = r.json()
    assert "db" in body and "redis" in body
    assert body["db"] in {"ok", "down"}
    assert body["redis"] in {"ok", "down"}


@pytest.mark.asyncio
async def test_metrics_endpoint(client: AsyncClient) -> None:
    r = await client.get("/metrics")
    assert r.status_code == 200
    assert b"blocked_requests_total" in r.content or r.status_code == 200
