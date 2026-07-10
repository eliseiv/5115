"""Integration tests for BYOK status reporting + policy gating (ADR-016, byok/02).

Covers: keyStatus across all six values; activeModel reported only when valid; set with a 401
→ invalid; set with a network/non-401 → offline; runtime 401 in /chat/run(byok) →
mark_expired (keyStatus=expired) and block byok_invalid; policy byokEnabled==enabled&&valid;
toggle never enables unless valid.

Status surfaces through the set/toggle/delete responses and /v1/policy/effective (there is no
separate GET /v1/byok/status route); we drive the public endpoints and assert on those.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import FakeAnthropicClient, auth_headers, seed_user


async def _set_status(maker: async_sessionmaker[AsyncSession], uid: uuid.UUID, status: str) -> None:
    """Force the stored byok_key_status (exercising statuses not reachable via set())."""
    async with maker() as s:
        await s.execute(
            text("UPDATE byok_keys SET key_status = CAST(:st AS byok_key_status) WHERE user_id=:u"),
            {"st": status, "u": str(uid)},
        )
        await s.commit()


# --- set → status mapping (valid / invalid / offline) ---
@pytest.mark.asyncio
async def test_set_valid_reports_valid_and_active_model(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    fake_anthropic.valid_keys = {"sk-ant-good"}
    r = await client.post(
        "/v1/byok/set",
        json={"userId": str(uid), "apiKey": "sk-ant-good"},
        headers=auth_headers(uid),
    )
    body = r.json()
    assert body["keyStatus"] == "valid"
    assert body["activeModel"] is not None  # activeModel present only when valid


@pytest.mark.asyncio
async def test_set_401_reports_invalid_no_active_model(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    fake_anthropic.valid_keys = set()  # 401 → invalid
    r = await client.post(
        "/v1/byok/set",
        json={"userId": str(uid), "apiKey": "sk-ant-bad"},
        headers=auth_headers(uid),
    )
    body = r.json()
    assert body["keyStatus"] == "invalid"
    assert body["byokEnabled"] is False
    assert body["activeModel"] is None


@pytest.mark.asyncio
async def test_set_network_error_reports_offline(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    fake_anthropic.offline_keys = {"sk-ant-net"}
    r = await client.post(
        "/v1/byok/set",
        json={"userId": str(uid), "apiKey": "sk-ant-net"},
        headers=auth_headers(uid),
    )
    body = r.json()
    assert body["keyStatus"] == "offline"  # network/non-401, not a 401
    assert body["byokEnabled"] is False
    assert body["activeModel"] is None


# --------------------------- all six keyStatus values are representable ---------------------------
@pytest.mark.asyncio
async def test_all_six_key_statuses_surface(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    # missing: no key set → policy/effective reports byokEnabled false, byok cannot generate.
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    eff_missing = await client.get("/v1/policy/effective", headers=auth_headers(uid))
    assert eff_missing.json()["byokEnabled"] is False

    # valid: set a good key.
    fake_anthropic.valid_keys = {"sk-ant-good"}
    set_resp = await client.post(
        "/v1/byok/set",
        json={"userId": str(uid), "apiKey": "sk-ant-good"},
        headers=auth_headers(uid),
    )
    assert set_resp.json()["keyStatus"] == "valid"

    # invalid / validating / offline / expired: drive each via stored status + toggle, asserting
    # toggle never enables for a non-valid status (ADR-016).
    for status in ("invalid", "validating", "offline", "expired"):
        await _set_status(db_sessionmaker, uid, status)
        t = await client.post(
            "/v1/byok/toggle",
            json={"userId": str(uid), "enabled": True},
            headers=auth_headers(uid),
        )
        body = t.json()
        assert body["keyStatus"] == status
        assert body["byokEnabled"] is False  # non-valid never enables
        assert body["activeModel"] is None


# --------------------------- toggle gating: enables only when valid ---------------------------
@pytest.mark.asyncio
async def test_toggle_enables_only_when_valid(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    fake_anthropic.valid_keys = {"sk-ant-good"}
    await client.post(
        "/v1/byok/set",
        json={"userId": str(uid), "apiKey": "sk-ant-good"},
        headers=auth_headers(uid),
    )
    t = await client.post(
        "/v1/byok/toggle",
        json={"userId": str(uid), "enabled": True},
        headers=auth_headers(uid),
    )
    assert t.json()["byokEnabled"] is True
    assert t.json()["keyStatus"] == "valid"


# --------------------------- policy: byokEnabled == enabled && valid ---------------------------
@pytest.mark.asyncio
async def test_policy_byok_enabled_requires_enabled_and_valid(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # enabled + valid → policy reports byok usable.
    async with db_sessionmaker() as s:
        uid_ok = await seed_user(
            s, subscription="active", balance=0, byok_enabled=True, byok_status="valid"
        )
    eff_ok = await client.get("/v1/policy/effective", headers=auth_headers(uid_ok))
    assert eff_ok.json()["byokEnabled"] is True
    assert eff_ok.json()["canGenerateByokMode"] is True

    # enabled flag but status invalid → not usable.
    async with db_sessionmaker() as s:
        uid_bad = await seed_user(
            s, subscription="active", balance=0, byok_enabled=True, byok_status="invalid"
        )
    eff_bad = await client.get("/v1/policy/effective", headers=auth_headers(uid_bad))
    assert eff_bad.json()["canGenerateByokMode"] is False
    assert "byok_invalid" in eff_bad.json()["reasons"]


# --- runtime 401 in /chat/run(byok) → expired + block ---
@pytest.mark.asyncio
async def test_runtime_401_marks_expired_and_blocks_byok(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(
            s, subscription="active", balance=0, byok_enabled=True, byok_status="valid"
        )
    # Anthropic rejects the stored BYOK key at runtime (401).
    fake_anthropic.auth_error_keys = {"sk-ant-user-key"}
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "projectId": "p", "message": "hi", "mode": "byok"},
        headers=auth_headers(uid),
    )
    assert r.status_code == 200
    assert r.json()["status"] == "blocked"
    assert r.json()["blockReason"] == "byok_invalid"

    # The key was previously valid → marked expired (not freshly invalid), byok disabled.
    async with db_sessionmaker() as s:
        row = await s.execute(
            text("SELECT key_status, enabled FROM byok_keys WHERE user_id=:u"), {"u": str(uid)}
        )
        status, enabled = row.one()
    assert status == "expired"
    assert enabled is False

    # A follow-up policy evaluation cannot generate via byok. mark_expired also disables byok,
    # and the policy state machine resolves byok_disabled BEFORE byok_invalid (BR-4 order:
    # byok_disabled → byok_invalid). So the effective reason here is byok_disabled, while the
    # in-flight /chat/run block above was byok_invalid (status was still enabled at decision time).
    eff = await client.get("/v1/policy/effective", headers=auth_headers(uid))
    assert eff.json()["canGenerateByokMode"] is False
    assert "byok_disabled" in eff.json()["reasons"]
