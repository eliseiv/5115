"""Integration: POST /v1/auth/apple — Sign in with Apple end-to-end (ADR-043).

Real PostgreSQL (testcontainers); the auth_identities table lives in migration 0012. Fully
hermetic: NO network to Apple — the verifier runs in test-mode (APPLE_TEST_MODE=true +
APPLE_TEST_SECRET), so identity tokens are HS256-signed with that secret and PyJWKClient is never
contacted. OUR issuer is configured (conftest's RSA key) so the round-trip access token verifies
through the existing JwtVerifier (self-consistent loop, ADR-018 §3). The per-IP rate limit is
forced to a deterministic decision (Redis-free hermetic suite).

The verifier reads get_settings() at construction; we override Settings (Apple + issuer fields)
and rebuild BOTH process-wide singletons (token issuer, Apple verifier) against it, restoring on
teardown so no other test sees a configured issuer / test-mode verifier.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt as pyjwt
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import _PRIVATE_PEM, _TEST_JWT_AUDIENCE, _TEST_JWT_ISSUER

_APPLE_SECRET = "apple-it-hs256-secret"  # noqa: S105 (test-only HS256 secret)
_APPLE_ISSUER = "https://appleid.apple.com"
_APPLE_AUD = "com.example.app"  # matches conftest APPSTORE_BUNDLE_ID; set explicitly below too


def _apple_token(
    *,
    sub: str,
    secret: str = _APPLE_SECRET,
    iss: str = _APPLE_ISSUER,
    aud: str = _APPLE_AUD,
    expired: bool = False,
    email: str | None = None,
    nonce_claim: str | None = None,
    alg: str = "HS256",
) -> str:
    now = datetime.now(UTC)
    exp = now - timedelta(hours=1) if expired else now + timedelta(hours=1)
    claims: dict[str, Any] = {
        "sub": sub,
        "iss": iss,
        "aud": aud,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }
    if email is not None:
        claims["email"] = email
        claims["email_verified"] = True
    if nonce_claim is not None:
        claims["nonce"] = nonce_claim
    return pyjwt.encode(claims, secret, algorithm=alg)


@pytest.fixture
async def apple_client(
    pg_url: str,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AsyncClient]:
    """ASGI client: OUR issuer configured + Apple verifier in test-mode + DB on the container."""
    from app import deps
    from app.api_gateway import auth as auth_mod
    from app.api_gateway import rate_limit
    from app.auth import apple as apple_mod
    from app.config import Settings, get_settings
    from app.main import create_app

    base = get_settings()
    configured = base.model_copy(
        update={
            # OUR issuer (sign access tokens that round-trip through the verify-only contour).
            "jwt_private_key": _PRIVATE_PEM,
            "jwt_private_key_path": "",
            "jwt_issuer": _TEST_JWT_ISSUER,
            "jwt_audience": _TEST_JWT_AUDIENCE,
            "jwt_kid": "test-kid-apple",
            "auth_jwks_enabled": True,
            # Apple verifier test-mode (no network to Apple).
            "apple_test_mode": True,
            "apple_test_secret": _APPLE_SECRET,
            "apple_oidc_issuer": _APPLE_ISSUER,
            "apple_audience": _APPLE_AUD,
        }
    )

    def _override_settings() -> Settings:
        return configured

    monkeypatch.setattr(deps, "get_settings", _override_settings)
    monkeypatch.setattr(auth_mod, "get_settings", _override_settings)
    monkeypatch.setattr(apple_mod, "get_settings", _override_settings)
    from app.api_gateway.routers import auth as auth_router

    monkeypatch.setattr(auth_router, "get_settings", _override_settings)

    # Rebuild process-wide singletons against the configured settings; restore on teardown.
    saved_issuer = deps._token_issuer_singleton
    saved_verifier = auth_mod._verifier_singleton
    saved_apple = apple_mod._verifier_singleton
    deps._token_issuer_singleton = None
    auth_mod._verifier_singleton = None
    apple_mod._verifier_singleton = None

    async def _allow(**_kwargs: Any) -> bool:
        return True

    orig_auth = rate_limit.enforce_auth_limits
    orig_other = rate_limit.enforce_other_limits
    monkeypatch.setattr(rate_limit, "enforce_auth_limits", _allow)
    monkeypatch.setattr(rate_limit, "enforce_other_limits", _allow)
    monkeypatch.setattr(auth_router, "enforce_auth_limits", _allow)

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
    apple_mod._verifier_singleton = saved_apple
    rate_limit.enforce_auth_limits = orig_auth  # type: ignore[assignment]
    rate_limit.enforce_other_limits = orig_other  # type: ignore[assignment]


# ----------------------------- happy path: shape + round-trip -----------------------------
@pytest.mark.asyncio
async def test_apple_sign_in_returns_full_token_pair(apple_client: AsyncClient) -> None:
    token = _apple_token(sub="apple-sub-happy", email="user@privaterelay.appleid.com")
    r = await apple_client.post(
        "/v1/auth/apple", json={"identityToken": token, "deviceId": "dev-apple-1"}
    )
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
    uuid.UUID(body["userId"])
    assert body["deviceId"] == "dev-apple-1"
    assert body["tokenType"] == "Bearer"
    assert body["accessToken"] and body["refreshToken"]


@pytest.mark.asyncio
async def test_apple_access_token_round_trips_through_verifier(apple_client: AsyncClient) -> None:
    token = _apple_token(sub="apple-sub-rt")
    r = await apple_client.post(
        "/v1/auth/apple", json={"identityToken": token, "deviceId": "dev-apple-rt"}
    )
    assert r.status_code == 200, r.text
    body = r.json()

    # OUR access token verifies through the existing JwtVerifier (sub==userId, device_id present).
    from app.api_gateway.auth import get_jwt_verifier

    verified = get_jwt_verifier().verify(body["accessToken"])
    assert str(verified.user_id) == body["userId"]
    assert verified.device_id == "dev-apple-rt"

    # And it authenticates a protected /v1/* endpoint (reaches business logic).
    r2 = await apple_client.get(
        "/v1/tools", headers={"Authorization": f"Bearer {body['accessToken']}"}
    )
    assert r2.status_code == 200, r2.text


@pytest.mark.asyncio
async def test_apple_without_device_id_generates_uuid4(apple_client: AsyncClient) -> None:
    token = _apple_token(sub="apple-sub-nodev")
    r = await apple_client.post("/v1/auth/apple", json={"identityToken": token})
    assert r.status_code == 200, r.text
    parsed = uuid.UUID(r.json()["deviceId"])
    assert parsed.version == 4


# ----------------------------- verification failures -> 401 -----------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "token_kwargs",
    [
        {"sub": "x", "secret": "wrong-secret"},  # bad signature
        {"sub": "x", "expired": True},  # expired
        {"sub": "x", "iss": "https://evil.example"},  # wrong issuer
        {"sub": "x", "aud": "com.someone.else"},  # wrong audience
    ],
)
async def test_apple_invalid_token_401(
    apple_client: AsyncClient, token_kwargs: dict[str, Any]
) -> None:
    token = _apple_token(**token_kwargs)
    r = await apple_client.post(
        "/v1/auth/apple", json={"identityToken": token, "deviceId": "dev-bad"}
    )
    assert r.status_code == 401, r.text
    assert r.json()["error"]["code"] == "unauthorized"


@pytest.mark.asyncio
async def test_apple_hs256_rejected_when_test_mode_off(
    apple_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Flip test-mode OFF on the live verifier: a valid-by-secret HS256 token is now 401
    # (no alg-confusion — HS256 is never accepted outside test-mode). The singleton is built
    # lazily on first request; materialize it (under the fixture's patched get_settings) first.
    from app.auth import apple as apple_mod

    monkeypatch.setattr(apple_mod.get_apple_verifier(), "_test_mode", False)
    token = _apple_token(sub="apple-sub-off")
    r = await apple_client.post(
        "/v1/auth/apple", json={"identityToken": token, "deviceId": "dev-off"}
    )
    assert r.status_code == 401, r.text


# ----------------------------- nonce policy -----------------------------
@pytest.mark.asyncio
async def test_apple_nonce_match_ok(apple_client: AsyncClient) -> None:
    import hashlib

    raw = "raw-nonce-1"
    token = _apple_token(
        sub="apple-sub-nonce-ok", nonce_claim=hashlib.sha256(raw.encode()).hexdigest()
    )
    r = await apple_client.post(
        "/v1/auth/apple", json={"identityToken": token, "deviceId": "dev-n1", "nonce": raw}
    )
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_apple_nonce_mismatch_401(apple_client: AsyncClient) -> None:
    import hashlib

    token = _apple_token(
        sub="apple-sub-nonce-bad",
        nonce_claim=hashlib.sha256(b"different").hexdigest(),
    )
    r = await apple_client.post(
        "/v1/auth/apple",
        json={"identityToken": token, "deviceId": "dev-n2", "nonce": "raw-nonce-1"},
    )
    assert r.status_code == 401, r.text


@pytest.mark.asyncio
async def test_apple_nonce_claim_absent_passes(apple_client: AsyncClient) -> None:
    # Client sends a nonce, token has no nonce claim => not checked (MVP optional) => 200.
    token = _apple_token(sub="apple-sub-nonce-skip")
    r = await apple_client.post(
        "/v1/auth/apple",
        json={"identityToken": token, "deviceId": "dev-n3", "nonce": "client-only"},
    )
    assert r.status_code == 200, r.text


# ----------------------------- linking: device account preserved -----------------------------
@pytest.mark.asyncio
async def test_apple_links_to_existing_device_account_preserves_data(
    apple_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # Anonymous device-account first (register), with a wallet balance to prove data is preserved.
    reg = await apple_client.post("/v1/auth/register", json={"deviceId": "dev-link"})
    assert reg.status_code == 200
    device_user_id = reg.json()["userId"]
    async with db_sessionmaker() as s:
        await s.execute(
            text("INSERT INTO wallets (user_id, balance) VALUES (:u, 500)"),
            {"u": device_user_id},
        )
        await s.commit()

    # First Apple sign-in from THE SAME device, no prior Apple identity => link to device user.
    token = _apple_token(sub="apple-sub-link", email="link@a.com")
    r = await apple_client.post(
        "/v1/auth/apple", json={"identityToken": token, "deviceId": "dev-link"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["userId"] == device_user_id  # same account (credits/history kept)

    async with db_sessionmaker() as s:
        # The identity row links to the device user; email is stored on creation.
        row = await s.execute(
            text(
                "SELECT user_id, email FROM auth_identities "
                "WHERE provider = 'apple' AND subject = 'apple-sub-link'"
            )
        )
        rec = row.mappings().one()
        assert str(rec["user_id"]) == device_user_id
        assert rec["email"] == "link@a.com"
        # Wallet balance preserved on the same user.
        bal = await s.execute(
            text("SELECT balance FROM wallets WHERE user_id = :u"), {"u": device_user_id}
        )
        assert bal.scalar_one() == 500


@pytest.mark.asyncio
async def test_apple_cross_device_same_user(apple_client: AsyncClient) -> None:
    # Same apple_sub on a DIFFERENT device => the same userId (cross-device account).
    token1 = _apple_token(sub="apple-sub-xdev")
    first = await apple_client.post(
        "/v1/auth/apple", json={"identityToken": token1, "deviceId": "dev-x-A"}
    )
    assert first.status_code == 200
    uid = first.json()["userId"]

    token2 = _apple_token(sub="apple-sub-xdev")
    second = await apple_client.post(
        "/v1/auth/apple", json={"identityToken": token2, "deviceId": "dev-x-B"}
    )
    assert second.status_code == 200
    assert second.json()["userId"] == uid
    assert second.json()["deviceId"] == "dev-x-B"


@pytest.mark.asyncio
async def test_apple_unknown_sub_no_device_creates_new_user(apple_client: AsyncClient) -> None:
    # Unknown apple_sub, no deviceId => brand-new device + brand-new user.
    t1 = _apple_token(sub="apple-sub-new-A")
    r1 = await apple_client.post("/v1/auth/apple", json={"identityToken": t1})
    t2 = _apple_token(sub="apple-sub-new-B")
    r2 = await apple_client.post("/v1/auth/apple", json={"identityToken": t2})
    assert r1.status_code == r2.status_code == 200
    assert r1.json()["userId"] != r2.json()["userId"]


@pytest.mark.asyncio
async def test_apple_device_already_has_identity_creates_new_user(
    apple_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # A device whose user already owns ONE Apple identity; a DIFFERENT apple_sub on the same device
    # must NOT hijack that account — it creates a new user (device re-pointed to it, ADR-043 §5).
    t_first = _apple_token(sub="apple-sub-occupant")
    first = await apple_client.post(
        "/v1/auth/apple", json={"identityToken": t_first, "deviceId": "dev-occupied"}
    )
    assert first.status_code == 200
    occupant_uid = first.json()["userId"]

    t_second = _apple_token(sub="apple-sub-intruder")
    second = await apple_client.post(
        "/v1/auth/apple", json={"identityToken": t_second, "deviceId": "dev-occupied"}
    )
    assert second.status_code == 200
    intruder_uid = second.json()["userId"]
    assert intruder_uid != occupant_uid  # new user, not the occupant's

    async with db_sessionmaker() as s:
        # Device is now bound to the intruder (apple_sub-user wins; no auto-merge).
        dev = await s.execute(
            text("SELECT user_id FROM auth_devices WHERE device_id = 'dev-occupied'")
        )
        assert str(dev.scalar_one()) == intruder_uid
        # The occupant's identity is untouched and still points at the occupant.
        occ = await s.execute(
            text(
                "SELECT user_id FROM auth_identities "
                "WHERE provider = 'apple' AND subject = 'apple-sub-occupant'"
            )
        )
        assert str(occ.scalar_one()) == occupant_uid


# ----------------------------- idempotency: no duplicate identity rows -------------------------
@pytest.mark.asyncio
async def test_apple_repeat_sign_in_idempotent_no_duplicates(
    apple_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    token = _apple_token(sub="apple-sub-idem")
    first = await apple_client.post(
        "/v1/auth/apple", json={"identityToken": token, "deviceId": "dev-idem-apple"}
    )
    second = await apple_client.post(
        "/v1/auth/apple",
        json={"identityToken": _apple_token(sub="apple-sub-idem")},
    )
    assert first.status_code == second.status_code == 200
    assert first.json()["userId"] == second.json()["userId"]

    async with db_sessionmaker() as s:
        count = await s.execute(
            text(
                "SELECT COUNT(*) FROM auth_identities "
                "WHERE provider = 'apple' AND subject = 'apple-sub-idem'"
            )
        )
        assert count.scalar_one() == 1


# ----------------------------- 503 when audience not configured -----------------------------
@pytest.mark.asyncio
async def test_apple_audience_not_configured_503(
    apple_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Clear the resolved audience on the live verifier => "not configured" => 503. The singleton
    # is built lazily on first request; materialize it (patched get_settings) before patching.
    from app.auth import apple as apple_mod

    monkeypatch.setattr(apple_mod.get_apple_verifier(), "_audience", "")
    token = _apple_token(sub="apple-sub-503")
    r = await apple_client.post(
        "/v1/auth/apple", json={"identityToken": token, "deviceId": "dev-503"}
    )
    assert r.status_code == 503, r.text
    assert r.json()["error"]["code"] == "service_unavailable"


# ----------------------------- validation 422 -----------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"identityToken": ""},  # empty token
        {"identityToken": "x", "deviceId": "bad id with spaces"},  # invalid deviceId charset
        {"identityToken": "x", "unexpected": "field"},  # extra forbidden field
        {"deviceId": "dev-x"},  # missing required identityToken
    ],
)
async def test_apple_validation_422(apple_client: AsyncClient, body: dict[str, Any]) -> None:
    r = await apple_client.post("/v1/auth/apple", json=body)
    assert r.status_code == 422, r.text


# ----------------------------- rate limit 429 -----------------------------
@pytest.mark.asyncio
async def test_apple_rate_limit_429(
    apple_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api_gateway.routers import auth as auth_router

    async def _deny(**_kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(auth_router, "enforce_auth_limits", _deny)
    token = _apple_token(sub="apple-sub-rl")
    r = await apple_client.post(
        "/v1/auth/apple", json={"identityToken": token, "deviceId": "dev-rl"}
    )
    assert r.status_code == 429, r.text
    assert r.json()["error"]["code"] == "rate_limited"
