"""Integration tests for /v1/profile (profile module, 09-testing.md).

accountId is a deterministic display derivation of userId (XXXX-XXXX-XXXXX); displayName is
clamped to <= 80 chars and an empty string resets it to null. Data is strictly scoped to the
JWT sub.
"""

from __future__ import annotations

import re
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import auth_headers, seed_user

_ACCOUNT_ID_RE = re.compile(r"^\d{4}-\d{4}-[A-Z0-9]{5}$")


@pytest.mark.asyncio
async def test_get_profile_account_id_format_and_deterministic(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    r1 = await client.get("/v1/profile", headers=auth_headers(uid))
    assert r1.status_code == 200
    body1 = r1.json()
    assert _ACCOUNT_ID_RE.match(body1["accountId"]), body1["accountId"]
    assert body1["displayName"] is None
    assert "createdAt" in body1

    # accountId is a pure derivation of userId → stable across calls.
    r2 = await client.get("/v1/profile", headers=auth_headers(uid))
    assert r2.json()["accountId"] == body1["accountId"]


@pytest.mark.asyncio
async def test_patch_sets_name_and_get_reflects(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    r = await client.patch(
        "/v1/profile", json={"displayName": "Ada Lovelace"}, headers=auth_headers(uid)
    )
    assert r.status_code == 200
    assert r.json()["displayName"] == "Ada Lovelace"
    # subsequent GET reflects the saved name.
    g = await client.get("/v1/profile", headers=auth_headers(uid))
    assert g.json()["displayName"] == "Ada Lovelace"


@pytest.mark.asyncio
async def test_patch_empty_string_resets_to_null(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    await client.patch("/v1/profile", json={"displayName": "Temp"}, headers=auth_headers(uid))
    r = await client.patch("/v1/profile", json={"displayName": "   "}, headers=auth_headers(uid))
    assert r.status_code == 200
    assert r.json()["displayName"] is None


@pytest.mark.asyncio
async def test_patch_name_too_long_422(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    r = await client.patch("/v1/profile", json={"displayName": "x" * 81}, headers=auth_headers(uid))
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_patch_extra_field_forbidden_422(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    r = await client.patch(
        "/v1/profile",
        json={"displayName": "ok", "isAdmin": True},
        headers=auth_headers(uid),
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_profile_is_scoped_to_sub(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        a = await seed_user(s)
        b = await seed_user(s)
    await client.patch("/v1/profile", json={"displayName": "User A"}, headers=auth_headers(a))
    await client.patch("/v1/profile", json={"displayName": "User B"}, headers=auth_headers(b))
    # each token only ever sees its own profile (data scoped to sub).
    ra = await client.get("/v1/profile", headers=auth_headers(a))
    rb = await client.get("/v1/profile", headers=auth_headers(b))
    assert ra.json()["displayName"] == "User A"
    assert rb.json()["displayName"] == "User B"
    assert ra.json()["accountId"] != rb.json()["accountId"]


@pytest.mark.asyncio
async def test_get_profile_requires_auth(client: AsyncClient) -> None:
    r = await client.get("/v1/profile")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_get_profile_unknown_user_provisioned_or_404(
    client: AsyncClient,
) -> None:
    """A token whose sub has no users row: provisioning (ADR-007) creates it lazily, OR a
    defensive 404. Either way it must not 500."""
    ghost = uuid.uuid4()
    r = await client.get("/v1/profile", headers=auth_headers(ghost))
    assert r.status_code in (200, 404)
