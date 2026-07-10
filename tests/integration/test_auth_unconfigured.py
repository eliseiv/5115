"""Integration: issuer-unconfigured 503 + per-IP rate-limit 429 for /v1/auth/* (ADR-018 §6/§7).

The default hermetic suite leaves the embedded issuer UNCONFIGURED (conftest forces JWT_PUBLIC_KEY
but never sets JWT_PRIVATE_KEY/_PATH), so the shared `client` fixture exercises the verify-only
posture: register/token/refresh return 503 while the verify-only /v1/* contour still works on the
public key. The 429 path is forced via a patched limiter (Redis-free suite fails open otherwise).
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import auth_headers, seed_user


# ----------------------------- 503 when issuer unconfigured -----------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/v1/auth/register", {"deviceId": "d503"}),
        ("/v1/auth/token", {"deviceId": "d503"}),
        ("/v1/auth/refresh", {"refreshToken": "whatever"}),
    ],
)
async def test_issuer_unconfigured_returns_503(
    client: AsyncClient, path: str, body: dict[str, Any]
) -> None:
    # No JWT_PRIVATE_KEY in the hermetic env → TokenIssuer.configured is False → 503.
    r = await client.post(path, json=body)
    assert r.status_code == 503, r.text
    assert r.json()["error"]["code"] == "service_unavailable"


@pytest.mark.asyncio
async def test_verify_only_v1_still_works_when_issuer_unconfigured(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # Verify-only contour: an externally-shaped token (conftest signs with the matching key) still
    # authenticates /v1/* even though the issuer endpoints are 503.
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    r = await client.get("/v1/tools", headers=auth_headers(uid))
    assert r.status_code == 200, r.text
    assert "tools" in r.json()


# ----------------------------- per-IP rate limit 429 -----------------------------
@pytest.mark.asyncio
async def test_auth_rate_limit_returns_429(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Force the per-IP limiter to deny on the auth router; the route must surface 429 before any
    # issuer work (so this holds regardless of the 503 issuer posture).
    from app.api_gateway.routers import auth as auth_router

    async def _deny(**_kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(auth_router, "enforce_auth_limits", _deny)
    r = await client.post("/v1/auth/register", json={"deviceId": "dratelimit"})
    assert r.status_code == 429, r.text
    assert r.json()["error"]["code"] == "rate_limited"


@pytest.mark.asyncio
async def test_auth_jwks_disabled_returns_404(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # AUTH_JWKS_ENABLED=false → GET /v1/auth/jwks is 404 (ADR-018 §2, auth/02).
    from app.api_gateway.routers import auth as auth_router
    from app.config import get_settings

    disabled = get_settings().model_copy(update={"auth_jwks_enabled": False})
    monkeypatch.setattr(auth_router, "get_settings", lambda: disabled)
    r = await client.get("/v1/auth/jwks")
    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_auth_jwks_no_public_key_returns_404(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Public key not configured → 404 even when the toggle is on.
    from app.api_gateway.routers import auth as auth_router
    from app.config import get_settings

    no_pub = get_settings().model_copy(
        update={"auth_jwks_enabled": True, "jwt_public_key": "", "jwt_public_key_path": ""}
    )
    monkeypatch.setattr(auth_router, "get_settings", lambda: no_pub)
    r = await client.get("/v1/auth/jwks")
    assert r.status_code == 404, r.text


# ----------------------------- per-IP limiter unit-level behavior (auth bucket) ----------------
@pytest.mark.asyncio
async def test_enforce_auth_limits_blocks_after_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Direct limiter check: the per-IP auth bucket allows up to AUTH_RATE_LIMIT_PER_IP (default 10)
    # then denies. Uses an in-memory fake redis (no live infra) mirroring test_rate_limit.py.
    from app.api_gateway import rate_limit

    class _FakePipeline:
        def __init__(self, counts: dict[str, int]) -> None:
            self._counts = counts
            self._ops: list[tuple[str, Any]] = []

        def zremrangebyscore(self, key: str, *_a: Any) -> None:
            self._ops.append(("noop", None))

        def zadd(self, key: str, mapping: dict[str, float]) -> None:
            self._counts[key] = self._counts.get(key, 0) + 1
            self._ops.append(("add", key))

        def zcard(self, key: str) -> None:
            self._ops.append(("card", key))

        def expire(self, key: str, _ttl: int) -> None:
            self._ops.append(("noop", None))

        async def execute(self) -> list[Any]:
            return [self._counts.get(k, 0) if op == "card" else None for op, k in self._ops]

        async def __aenter__(self) -> _FakePipeline:
            return self

        async def __aexit__(self, *_a: Any) -> None:
            return None

    class _FakeRedis:
        def __init__(self) -> None:
            self._counts: dict[str, int] = {}

        def pipeline(self, transaction: bool = True) -> _FakePipeline:
            return _FakePipeline(self._counts)

    fake = _FakeRedis()  # one instance: the sliding-window counter must accumulate across calls.
    monkeypatch.setattr(rate_limit, "get_redis", lambda: fake)
    ip = "203.0.113.5"
    results = [await rate_limit.enforce_auth_limits(ip=ip) for _ in range(11)]
    assert results[:10] == [True] * 10
    assert results[10] is False
