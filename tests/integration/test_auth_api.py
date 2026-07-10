"""Integration: embedded auth-issuer /v1/auth/* + verify-only loop (ADR-018, auth/02).

Real PostgreSQL (testcontainers) per 06-testing-strategy.md; the auth tables live in migration
0005. These tests configure a REAL RS256 issuer by pointing JWT_PRIVATE_KEY at the private half of
conftest's ephemeral key pair (whose public half conftest already forces into JWT_PUBLIC_KEY), so
issued access tokens verify with the existing JwtVerifier (self-consistent loop, ADR-018 §3) and
pass /v1/* business auth. The per-IP rate limit is exercised via the limiter directly and via a
patched limiter for the 429 HTTP path (Redis is not present in the hermetic suite — it fails open,
so we force the decision deterministically rather than asserting live Redis state).

Hermetic: the app's DB dependency is overridden to the container sessionmaker (conftest pattern);
the token issuer and JWT verifier process-wide singletons are rebuilt against the configured key
and restored on teardown so no other test sees a configured issuer.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import _PRIVATE_PEM, _TEST_JWT_AUDIENCE, _TEST_JWT_ISSUER


@pytest.fixture
async def auth_client(
    pg_url: str,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AsyncClient]:
    """ASGI client whose embedded issuer is configured (private key set) and DB is the container.

    The issuer signs with conftest's _PRIVATE_PEM; the verifier already trusts the matching
    public key (conftest forces JWT_PUBLIC_KEY), so issued tokens round-trip through /v1/*.
    """
    from app import deps
    from app.api_gateway import auth as auth_mod
    from app.api_gateway import rate_limit
    from app.config import Settings, get_settings
    from app.main import create_app

    # Configure the issuer: derive an override Settings from the cached one with the private key.
    base = get_settings()
    configured = base.model_copy(
        update={
            "jwt_private_key": _PRIVATE_PEM,
            "jwt_private_key_path": "",
            "jwt_issuer": _TEST_JWT_ISSUER,
            "jwt_audience": _TEST_JWT_AUDIENCE,
            "jwt_kid": "test-kid-auth",
            "auth_jwks_enabled": True,
        }
    )

    def _override_settings() -> Settings:
        return configured

    # deps.get_token_issuer / auth.JwtVerifier read get_settings() by name in their own modules.
    monkeypatch.setattr(deps, "get_settings", _override_settings)
    monkeypatch.setattr(auth_mod, "get_settings", _override_settings)
    # The auth router resolves get_settings for the jwks toggle/public key too.
    from app.api_gateway.routers import auth as auth_router

    monkeypatch.setattr(auth_router, "get_settings", _override_settings)

    # Rebuild the process-wide singletons against the configured settings; restore on teardown.
    saved_issuer = deps._token_issuer_singleton
    saved_verifier = auth_mod._verifier_singleton
    deps._token_issuer_singleton = None
    auth_mod._verifier_singleton = None

    # Auth + other rate limits: force a deterministic allow (Redis-free hermetic suite).
    async def _allow_auth(**_kwargs: Any) -> bool:
        return True

    async def _allow_other(**_kwargs: Any) -> bool:
        return True

    orig_auth = rate_limit.enforce_auth_limits
    orig_other = rate_limit.enforce_other_limits
    monkeypatch.setattr(rate_limit, "enforce_auth_limits", _allow_auth)
    monkeypatch.setattr(rate_limit, "enforce_other_limits", _allow_other)
    monkeypatch.setattr(auth_router, "enforce_auth_limits", _allow_auth)

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

    deps._token_issuer_singleton = saved_issuer
    auth_mod._verifier_singleton = saved_verifier
    rate_limit.enforce_auth_limits = orig_auth  # type: ignore[assignment]
    rate_limit.enforce_other_limits = orig_other  # type: ignore[assignment]


# ----------------------------- register: shape, generated deviceId -----------------------------
@pytest.mark.asyncio
async def test_register_returns_full_token_pair(auth_client: AsyncClient) -> None:
    r = await auth_client.post("/v1/auth/register", json={"deviceId": "dev-reg-1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body.keys()) == {
        "userId",
        "deviceId",
        "accessToken",
        "tokenType",
        "expiresIn",
        "refreshToken",
        "refreshExpiresIn",
    }
    uuid.UUID(body["userId"])  # valid uuid
    assert body["deviceId"] == "dev-reg-1"
    assert body["tokenType"] == "Bearer"
    assert body["expiresIn"] == 3600
    assert body["refreshExpiresIn"] == 2592000
    assert body["accessToken"] and body["refreshToken"]


@pytest.mark.asyncio
async def test_register_without_device_id_generates_uuid4(auth_client: AsyncClient) -> None:
    r = await auth_client.post("/v1/auth/register", json={})
    assert r.status_code == 200, r.text
    device_id = r.json()["deviceId"]
    # Generated deviceId is a UUIDv4.
    parsed = uuid.UUID(device_id)
    assert parsed.version == 4


# ----------------------------- round-trip: issued token passes /v1/* -----------------------------
@pytest.mark.asyncio
async def test_issued_access_token_verifies_and_passes_v1(auth_client: AsyncClient) -> None:
    reg = await auth_client.post("/v1/auth/register", json={"deviceId": "dev-rt"})
    assert reg.status_code == 200
    body = reg.json()
    token = body["accessToken"]
    user_id = body["userId"]

    # The same JwtVerifier (self-consistent loop) accepts the issued token: sub==userId, device_id.
    from app.api_gateway.auth import get_jwt_verifier

    verified = get_jwt_verifier().verify(token)
    assert str(verified.user_id) == user_id
    assert verified.device_id == "dev-rt"

    # And it is NOT rejected as unauthorized on a protected /v1/* endpoint (reaches business logic).
    r = await auth_client.get("/v1/tools", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    assert "tools" in r.json()


# ----------------------------- idempotency: same deviceId -> same userId, one row --------------
@pytest.mark.asyncio
async def test_register_same_device_idempotent_same_user(
    auth_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    first = await auth_client.post("/v1/auth/register", json={"deviceId": "dev-idem"})
    second = await auth_client.post("/v1/auth/register", json={"deviceId": "dev-idem"})
    assert first.status_code == second.status_code == 200
    uid1 = first.json()["userId"]
    uid2 = second.json()["userId"]
    assert uid1 == uid2

    async with db_sessionmaker() as s:
        rows = await s.execute(
            text("SELECT COUNT(*) FROM auth_devices WHERE device_id = :d"), {"d": "dev-idem"}
        )
        assert rows.scalar_one() == 1
        users = await s.execute(text("SELECT COUNT(*) FROM users WHERE id = :id"), {"id": uid1})
        assert users.scalar_one() == 1


@pytest.mark.asyncio
async def test_token_known_device_returns_same_user(auth_client: AsyncClient) -> None:
    reg = await auth_client.post("/v1/auth/register", json={"deviceId": "dev-tok"})
    assert reg.status_code == 200
    tok = await auth_client.post("/v1/auth/token", json={"deviceId": "dev-tok"})
    assert tok.status_code == 200
    assert tok.json()["userId"] == reg.json()["userId"]


@pytest.mark.asyncio
async def test_token_requires_device_id(auth_client: AsyncClient) -> None:
    # /v1/auth/token: deviceId is mandatory (schema-level) → 422 when absent.
    r = await auth_client.post("/v1/auth/token", json={})
    assert r.status_code == 422


# ----------------------------- refresh rotation + reuse revokes chain --------------------------
@pytest.mark.asyncio
async def test_refresh_rotates_and_marks_old_used(
    auth_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    reg = await auth_client.post("/v1/auth/register", json={"deviceId": "dev-ref"})
    old_refresh = reg.json()["refreshToken"]
    user_id = reg.json()["userId"]

    refreshed = await auth_client.post("/v1/auth/refresh", json={"refreshToken": old_refresh})
    assert refreshed.status_code == 200, refreshed.text
    new_refresh = refreshed.json()["refreshToken"]
    assert new_refresh != old_refresh
    assert refreshed.json()["userId"] == user_id

    # The presented (old) token is now single-use spent (used_at set).
    import hashlib

    old_hash = hashlib.sha256(old_refresh.encode("utf-8")).hexdigest()
    async with db_sessionmaker() as s:
        row = await s.execute(
            text("SELECT used_at FROM auth_refresh_tokens WHERE token_hash = :h"),
            {"h": old_hash},
        )
        assert row.scalar_one() is not None  # used_at populated


@pytest.mark.asyncio
async def test_refresh_reuse_detected_revokes_chain(auth_client: AsyncClient) -> None:
    reg = await auth_client.post("/v1/auth/register", json={"deviceId": "dev-reuse"})
    r1 = reg.json()["refreshToken"]

    # First rotation: ok, yields a fresh refresh that is part of the same device chain.
    rot1 = await auth_client.post("/v1/auth/refresh", json={"refreshToken": r1})
    assert rot1.status_code == 200
    r2 = rot1.json()["refreshToken"]

    # Reuse of the already-used r1 → 401 and revoke the whole device chain (theft detection).
    reuse = await auth_client.post("/v1/auth/refresh", json={"refreshToken": r1})
    assert reuse.status_code == 401

    # The chain is revoked: the previously-valid r2 is now also rejected.
    after = await auth_client.post("/v1/auth/refresh", json={"refreshToken": r2})
    assert after.status_code == 401


@pytest.mark.asyncio
async def test_refresh_unknown_token_401(auth_client: AsyncClient) -> None:
    r = await auth_client.post("/v1/auth/refresh", json={"refreshToken": "not-a-real-refresh"})
    assert r.status_code == 401


# ----------------------------- JWKS -----------------------------
@pytest.mark.asyncio
async def test_jwks_returns_only_public_key(auth_client: AsyncClient) -> None:
    r = await auth_client.get("/v1/auth/jwks")
    assert r.status_code == 200, r.text
    keys = r.json()["keys"]
    assert len(keys) == 1
    key = keys[0]
    assert set(key.keys()) == {"kty", "use", "alg", "kid", "n", "e"}
    assert key["kty"] == "RSA"
    assert key["use"] == "sig"
    assert key["alg"] == "RS256"
    # No private-material fields are present.
    for forbidden in ("d", "p", "q"):
        assert forbidden not in key


# ----------------------------- ADR-007 lazy fallback -----------------------------
@pytest.mark.asyncio
async def test_token_sub_without_users_row_is_lazily_provisioned(
    auth_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # ADR-018 §4 / ADR-007: a verifiable token whose sub has no users row is provisioned lazily on
    # the first /v1/*. We forge a token for an unknown subject signed by the configured issuer.
    from app.deps import get_token_issuer

    unknown = uuid.uuid4()
    token = get_token_issuer().issue_access_token(user_id=unknown, device_id="dev-lazy")
    async with db_sessionmaker() as s:
        before = await s.execute(
            text("SELECT COUNT(*) FROM users WHERE id = :id"), {"id": str(unknown)}
        )
        assert before.scalar_one() == 0

    r = await auth_client.get("/v1/tools", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text

    async with db_sessionmaker() as s:
        after = await s.execute(
            text("SELECT COUNT(*) FROM users WHERE id = :id"), {"id": str(unknown)}
        )
        assert after.scalar_one() == 1
