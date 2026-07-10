"""Integration: session-fixed model in /chat/run + orchestrator→client proboros (ADR-034 §3,§4).

Real PostgreSQL container, Anthropic faked at the client boundary. The fake records the `model`
kwarg the orchestrator hands to create_message (conftest), so we assert model proboros directly.

Covers:
- create session with a valid model from the allowlist → chat_sessions.model written + passed to
  create_message(model=<id>);
- create without model → chat_sessions.model NULL → create_message(model=None) → client default;
- resume with a DIFFERENT model in the body → ignored (session model governs, not an error);
- resume with an INVALID model in the body → ignored (no 422, no rewrite of the stored model);
- create with an unknown model → 422, no session written, no upstream call;
- blank model → 422 (schema), no session, no upstream call;
- billing unchanged: 1 credit regardless of the selected model.

The allowlist is configured by mutating the cached Settings singleton (restored per test).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from tests.conftest import FakeAnthropicClient, auth_headers, seed_user


@pytest.fixture
def restore_model_settings() -> Iterator[None]:
    s = get_settings()
    orig = (s.llm_provider, s.anthropic_models_raw, s.anthropic_model)
    yield
    (s.llm_provider, s.anthropic_models_raw, s.anthropic_model) = orig


def _set_allowlist(raw: dict[str, str], *, default_model: str) -> None:
    s = get_settings()
    s.llm_provider = "anthropic"
    s.anthropic_models_raw = json.dumps(raw)
    s.anthropic_model = default_model


async def _session_model(maker: async_sessionmaker[AsyncSession], session_id: str) -> str | None:
    async with maker() as s:
        return await s.scalar(
            text("SELECT model FROM chat_sessions WHERE id=:sid"), {"sid": session_id}
        )


async def _count(maker: async_sessionmaker[AsyncSession], sql: str, uid: uuid.UUID) -> int:
    async with maker() as s:
        return int(await s.scalar(text(sql), {"u": str(uid)}) or 0)


# ----------------------------- create with valid model -----------------------------
@pytest.mark.asyncio
async def test_create_session_with_valid_model_persists_and_proboros(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    restore_model_settings: None,
) -> None:
    _set_allowlist(
        {"claude-sonnet-4-5": "Sonnet", "claude-opus": "Opus"}, default_model="claude-sonnet-4-5"
    )
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [fake_anthropic.text_result("ok")]

    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "hi", "mode": "credits", "model": "claude-opus"},
        headers=auth_headers(uid),
    )
    assert r.status_code == 200, r.text
    sess = r.json()["sessionId"]
    # Persisted on the session.
    assert await _session_model(db_sessionmaker, sess) == "claude-opus"
    # Handed to the client verbatim.
    assert fake_anthropic.calls[-1]["model"] == "claude-opus"


# ------------------------- create without model → NULL → client default -------------------------
@pytest.mark.asyncio
async def test_create_session_without_model_is_null_and_client_default(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    restore_model_settings: None,
) -> None:
    _set_allowlist({"claude-sonnet-4-5": "Sonnet"}, default_model="claude-sonnet-4-5")
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [fake_anthropic.text_result("ok")]

    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "hi", "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert r.status_code == 200, r.text
    sess = r.json()["sessionId"]
    # NULL = instance default; orchestrator passes model=None (client resolves its own default).
    assert await _session_model(db_sessionmaker, sess) is None
    assert fake_anthropic.calls[-1]["model"] is None


# ----------------------------- resume: different / invalid model in body → ignored ----------------
@pytest.mark.asyncio
async def test_resume_with_different_model_uses_session_value(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    restore_model_settings: None,
) -> None:
    _set_allowlist(
        {"claude-sonnet-4-5": "Sonnet", "claude-opus": "Opus"}, default_model="claude-sonnet-4-5"
    )
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [
        fake_anthropic.text_result("first"),
        fake_anthropic.text_result("second"),
    ]
    r1 = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "one", "mode": "credits", "model": "claude-opus"},
        headers=auth_headers(uid),
    )
    sess = r1.json()["sessionId"]

    # Resume with a DIFFERENT (valid) model in the body → ignored, session model (opus) governs.
    r2 = await client.post(
        "/v1/chat/run",
        json={
            "userId": str(uid),
            "sessionId": sess,
            "message": "two",
            "mode": "credits",
            "model": "claude-sonnet-4-5",
        },
        headers=auth_headers(uid),
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["status"] == "assistant_message"
    # Stored model unchanged; the resume call still uses the session's opus.
    assert await _session_model(db_sessionmaker, sess) == "claude-opus"
    assert fake_anthropic.calls[-1]["model"] == "claude-opus"


@pytest.mark.asyncio
async def test_resume_with_invalid_model_in_body_is_ignored_not_422(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    restore_model_settings: None,
) -> None:
    _set_allowlist({"claude-opus": "Opus"}, default_model="claude-sonnet-4-5")
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [
        fake_anthropic.text_result("first"),
        fake_anthropic.text_result("second"),
    ]
    r1 = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "one", "mode": "credits", "model": "claude-opus"},
        headers=auth_headers(uid),
    )
    sess = r1.json()["sessionId"]

    # Resume with an UNKNOWN model in the body → ignored on resume (NOT a 422), session governs.
    r2 = await client.post(
        "/v1/chat/run",
        json={
            "userId": str(uid),
            "sessionId": sess,
            "message": "two",
            "mode": "credits",
            "model": "totally-unknown-model",
        },
        headers=auth_headers(uid),
    )
    assert r2.status_code == 200, r2.text
    assert await _session_model(db_sessionmaker, sess) == "claude-opus"
    assert fake_anthropic.calls[-1]["model"] == "claude-opus"


# ----------------------------- create with unknown model → 422 -----------------------------
@pytest.mark.asyncio
async def test_create_with_unknown_model_is_422_no_session_no_upstream(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    restore_model_settings: None,
) -> None:
    _set_allowlist({"claude-sonnet-4-5": "Sonnet"}, default_model="claude-sonnet-4-5")
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)

    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "hi", "mode": "credits", "model": "gpt-9000"},
        headers=auth_headers(uid),
    )
    assert r.status_code == 422, r.text
    # ADR-034 §3 (unified up to ADR-055 §4): the allowlist gate returns the DEDICATED machine code
    # `unsupported_model` in the SAME body location (error.code) as `unsupported_dialog_mode` — read
    # identically to test_dialog_mode_adr055.test_unknown_dialog_mode_is_422_with_machine_code, so
    # the two named gates are provably symmetric (both 422, both error.code). NOT a generic 422.
    assert r.json()["error"]["code"] == "unsupported_model"
    # Validation runs before persist / upstream: no session, no Anthropic call, no debit.
    assert not fake_anthropic.calls
    assert (
        await _count(db_sessionmaker, "SELECT count(*) FROM chat_sessions WHERE user_id=:u", uid)
        == 0
    )
    assert (
        await _count(
            db_sessionmaker,
            "SELECT count(*) FROM ledger_transactions WHERE user_id=:u AND type='debit'",
            uid,
        )
        == 0
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("blank", ["", "   "])
async def test_create_with_blank_model_is_422_no_session(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    restore_model_settings: None,
    blank: str,
) -> None:
    _set_allowlist({"claude-sonnet-4-5": "Sonnet"}, default_model="claude-sonnet-4-5")
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "hi", "mode": "credits", "model": blank},
        headers=auth_headers(uid),
    )
    assert r.status_code == 422, r.text
    assert not fake_anthropic.calls
    assert (
        await _count(db_sessionmaker, "SELECT count(*) FROM chat_sessions WHERE user_id=:u", uid)
        == 0
    )


# ----------------------------- billing unchanged -----------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("model", [None, "claude-opus"])
async def test_one_credit_regardless_of_model(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    restore_model_settings: None,
    model: str | None,
) -> None:
    _set_allowlist(
        {"claude-sonnet-4-5": "Sonnet", "claude-opus": "Opus"}, default_model="claude-sonnet-4-5"
    )
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [fake_anthropic.text_result("ok")]
    payload: dict[str, object] = {"userId": str(uid), "message": "hi", "mode": "credits"}
    if model is not None:
        payload["model"] = model

    r = await client.post("/v1/chat/run", json=payload, headers=auth_headers(uid))
    assert r.json()["status"] == "assistant_message"
    async with db_sessionmaker() as s:
        bal = await s.scalar(text("SELECT balance FROM wallets WHERE user_id=:u"), {"u": str(uid)})
    assert int(bal) == 4  # exactly 1 credit, independent of the selected model
