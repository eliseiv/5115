"""End-to-end chat flows through the app (AC-1..AC-6, AC-9, AC-10 upstream).

Anthropic is faked at the client boundary (FakeAnthropicClient), DB is the real container.
Each test scripts the fake's responses to drive run → tool_call → tool-result → ... loop.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import FakeAnthropicClient, auth_headers, seed_user
from tests.fake_client_tool import (
    FAKE_CLIENT_MUTATING_TOOL,
    FAKE_CLIENT_TOOL,
    register_fake_client_tool,
)


@pytest.fixture(autouse=True)
def _register_fake_client_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    # ADR-063: no shipped client-side tool remains; register a plain + a mutating test-only
    # client-side example so the tool-loop / hand-off / mutation-audit flows are exercised.
    register_fake_client_tool(monkeypatch)
    register_fake_client_tool(monkeypatch, mutating=True)


async def _count(maker: async_sessionmaker[AsyncSession], sql: str, uid: uuid.UUID) -> int:
    async with maker() as s:
        row = await s.scalar(text(sql), {"u": str(uid)})
        return int(row or 0)


# --------------------------- AC-1: trial once, no debit ---------------------------
@pytest.mark.asyncio
async def test_trial_once_then_blocked(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, trial_used=False)  # no subscription, balance None→0

    fake_anthropic.responses = [fake_anthropic.text_result("first answer")]
    r1 = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "projectId": "p", "message": "hi", "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert r1.status_code == 200
    assert r1.json()["status"] == "assistant_message"

    # trial flipped, NO debit row, balance stays 0.
    debits = await _count(
        db_sessionmaker,
        "SELECT count(*) FROM ledger_transactions WHERE user_id=:u AND type='debit'",
        uid,
    )
    assert debits == 0
    async with db_sessionmaker() as s:
        tu = await s.scalar(text("SELECT trial_used FROM users WHERE id=:u"), {"u": str(uid)})
    assert tu is True

    # second run → blocked trial_used (HTTP 200).
    r2 = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "projectId": "p", "message": "again", "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert r2.status_code == 200
    assert r2.json()["status"] == "blocked"
    assert r2.json()["blockReason"] == "trial_used"


# --------------------------- AC-3: active + credits → 1 debit ---------------------------
@pytest.mark.asyncio
async def test_active_credits_single_debit(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [fake_anthropic.text_result("ok")]
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "projectId": "p", "message": "hi", "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert r.json()["status"] == "assistant_message"
    async with db_sessionmaker() as s:
        bal = await s.scalar(text("SELECT balance FROM wallets WHERE user_id=:u"), {"u": str(uid)})
    assert int(bal) == 4
    debits = await _count(
        db_sessionmaker,
        "SELECT count(*) FROM ledger_transactions WHERE user_id=:u AND type='debit'",
        uid,
    )
    assert debits == 1


@pytest.mark.asyncio
async def test_active_credits_zero_blocked(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=0)
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "projectId": "p", "message": "hi", "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert r.json()["status"] == "blocked"
    assert r.json()["blockReason"] == "credits_empty"
    assert not fake_anthropic.calls  # never called Anthropic


# --------------------------- AC-2: expired blocks both modes ---------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["credits", "byok"])
async def test_expired_blocks(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    mode: str,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(
            s, subscription="expired", balance=5, byok_enabled=True, byok_status="valid"
        )
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "projectId": "p", "message": "hi", "mode": mode},
        headers=auth_headers(uid),
    )
    assert r.json()["status"] == "blocked"
    assert r.json()["blockReason"] == "subscription_expired"


# --------------------------- AC-4: tool-loop multi-round + debit once ---------------------------
@pytest.mark.asyncio
async def test_tool_loop_multi_round_single_debit(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)

    # run → tool_call(round1); tool-result → tool_call(round2); tool-result → assistant_message
    fake_anthropic.responses = [
        fake_anthropic.tool_result(FAKE_CLIENT_TOOL, {"path": "a.txt"}),
        fake_anthropic.tool_result(FAKE_CLIENT_TOOL, {"path": ".", "recursive": False}),
        fake_anthropic.text_result("done"),
    ]

    r1 = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "projectId": "p", "message": "do it", "mode": "credits"},
        headers=auth_headers(uid),
    )
    b1 = r1.json()
    assert b1["status"] == "tool_call"
    sess = b1["sessionId"]
    tcid1 = b1["toolCall"]["id"]

    r2 = await client.post(
        "/v1/chat/tool-result",
        json={
            "userId": str(uid),
            "sessionId": sess,
            "toolCallId": tcid1,
            "result": {"ok": True},
        },
        headers=auth_headers(uid),
    )
    b2 = r2.json()
    assert b2["status"] == "tool_call"
    tcid2 = b2["toolCall"]["id"]

    r3 = await client.post(
        "/v1/chat/tool-result",
        json={
            "userId": str(uid),
            "sessionId": sess,
            "toolCallId": tcid2,
            "result": {"ok": True},
        },
        headers=auth_headers(uid),
    )
    b3 = r3.json()
    assert b3["status"] == "assistant_message"
    assert b3["assistantMessage"] == "done"

    # exactly ONE debit despite 3 Anthropic calls / 2 tool rounds.
    debits = await _count(
        db_sessionmaker,
        "SELECT count(*) FROM ledger_transactions WHERE user_id=:u AND type='debit'",
        uid,
    )
    assert debits == 1
    async with db_sessionmaker() as s:
        bal = await s.scalar(text("SELECT balance FROM wallets WHERE user_id=:u"), {"u": str(uid)})
    assert int(bal) == 4


@pytest.mark.asyncio
async def test_continuation_replays_raw_provider_tool_use_id(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """ADR-008 / BUG-4 regression: on continuation the tool_result.tool_use_id sent to Anthropic
    MUST be the raw provider id (toolu_...), NOT the domain toolCallId (uuid4); and the replayed
    assistant tool_use.id MUST also be that same raw provider id. This fails on the old
    implementation that used the domain uuid4 for tool_use_id (Anthropic 400 → backend 502).
    """
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)

    fake_anthropic.responses = [
        fake_anthropic.tool_result(FAKE_CLIENT_TOOL, {"path": "a.txt"}, tool_id="toolu_abc123"),
        fake_anthropic.text_result("final"),
    ]

    r1 = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "projectId": "p", "message": "go", "mode": "credits"},
        headers=auth_headers(uid),
    )
    b1 = r1.json()
    assert b1["status"] == "tool_call"
    sess = b1["sessionId"]
    domain_tcid = b1["toolCall"]["id"]
    # The public toolCallId is the domain UUID, distinct from the raw provider id.
    assert domain_tcid != "toolu_abc123"

    r2 = await client.post(
        "/v1/chat/tool-result",
        json={
            "userId": str(uid),
            "sessionId": sess,
            "toolCallId": domain_tcid,
            "result": {"ok": True},
        },
        headers=auth_headers(uid),
    )
    assert r2.json()["status"] == "assistant_message"

    # Inspect the continuation request actually sent to Anthropic.
    continuation_messages = fake_anthropic.calls[-1]["messages"]

    # 1) The tool_result block carries the RAW provider id, not the domain uuid4.
    tool_results = [
        block
        for msg in continuation_messages
        if isinstance(msg.get("content"), list)
        for block in msg["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]
    assert len(tool_results) == 1
    assert tool_results[0]["tool_use_id"] == "toolu_abc123"
    assert tool_results[0]["tool_use_id"] != domain_tcid

    # 2) The replayed assistant tool_use block carries the SAME raw provider id.
    tool_uses = [
        block
        for msg in continuation_messages
        if isinstance(msg.get("content"), list)
        for block in msg["content"]
        if isinstance(block, dict) and block.get("type") == "tool_use"
    ]
    assert len(tool_uses) == 1
    assert tool_uses[0]["id"] == "toolu_abc123"


@pytest.mark.asyncio
async def test_tool_result_idempotent_replay(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [
        fake_anthropic.tool_result(FAKE_CLIENT_TOOL, {"path": "a.txt"}),
        fake_anthropic.text_result("final"),
    ]
    r1 = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "projectId": "p", "message": "go", "mode": "credits"},
        headers=auth_headers(uid),
    )
    sess = r1.json()["sessionId"]
    tcid = r1.json()["toolCall"]["id"]

    payload = {"userId": str(uid), "sessionId": sess, "toolCallId": tcid, "result": {"ok": 1}}
    first = await client.post("/v1/chat/tool-result", json=payload, headers=auth_headers(uid))
    assert first.json()["status"] == "assistant_message"
    calls_after_first = len(fake_anthropic.calls)

    # Replay the SAME tool-result → must NOT call Anthropic again, returns saved step.
    second = await client.post("/v1/chat/tool-result", json=payload, headers=auth_headers(uid))
    assert second.json()["status"] == "assistant_message"
    assert second.json()["assistantMessage"] == "final"
    assert len(fake_anthropic.calls) == calls_after_first  # no extra upstream call


@pytest.mark.asyncio
async def test_tool_result_foreign_session_404(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
        other = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [fake_anthropic.tool_result(FAKE_CLIENT_TOOL, {"path": "a"})]
    r1 = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "projectId": "p", "message": "go", "mode": "credits"},
        headers=auth_headers(uid),
    )
    sess = r1.json()["sessionId"]
    tcid = r1.json()["toolCall"]["id"]

    # other user tries to complete uid's tool call.
    r = await client.post(
        "/v1/chat/tool-result",
        json={"userId": str(other), "sessionId": sess, "toolCallId": tcid, "result": {"x": 1}},
        headers=auth_headers(other),
    )
    assert r.status_code == 404


# --------------------------- AC-5: BYOK routing + mutation audit ---------------------------
@pytest.mark.asyncio
async def test_byok_mode_works_and_does_not_debit(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(
            s, subscription="active", balance=0, byok_enabled=True, byok_status="valid"
        )
    fake_anthropic.responses = [fake_anthropic.text_result("byok answer")]
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "projectId": "p", "message": "hi", "mode": "byok"},
        headers=auth_headers(uid),
    )
    assert r.json()["status"] == "assistant_message"
    # BYOK key forwarded to the client (per-call api_key set, not the service key).
    assert fake_anthropic.calls[-1]["api_key"] == "sk-ant-user-key"
    # No debit on BYOK.
    debits = await _count(
        db_sessionmaker,
        "SELECT count(*) FROM ledger_transactions WHERE user_id=:u AND type='debit'",
        uid,
    )
    assert debits == 0


@pytest.mark.asyncio
async def test_mutating_tool_writes_audit(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [
        fake_anthropic.tool_result(
            FAKE_CLIENT_MUTATING_TOOL,
            {"path": "a.txt", "content": "x", "encoding": "utf8", "overwrite": True},
        ),
        fake_anthropic.text_result("written"),
    ]
    r1 = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "projectId": "p", "message": "write", "mode": "credits"},
        headers=auth_headers(uid),
    )
    sess = r1.json()["sessionId"]
    tcid = r1.json()["toolCall"]["id"]
    await client.post(
        "/v1/chat/tool-result",
        json={"userId": str(uid), "sessionId": sess, "toolCallId": tcid, "result": {"ok": 1}},
        headers=auth_headers(uid),
    )
    mut = await _count(
        db_sessionmaker,
        "SELECT count(*) FROM audit_logs WHERE user_id=:u AND event_type='tool_mutation'",
        uid,
    )
    assert mut == 1


# --------------------------- AC-6: /policy/effective == /chat/run ---------------------------
@pytest.mark.asyncio
async def test_policy_effective_matches_chat_run(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=0)  # credits empty
    eff = await client.get("/v1/policy/effective", headers=auth_headers(uid))
    assert eff.json()["canGenerateCreditsMode"] is False

    run = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "projectId": "p", "message": "hi", "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert run.json()["status"] == "blocked"  # consistent with effective=False


# --------------------------- AC-10: upstream 502 ---------------------------
@pytest.mark.asyncio
async def test_anthropic_upstream_error_502(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.raise_upstream = True
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "projectId": "p", "message": "hi", "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert r.status_code == 502
    # No debit on upstream failure.
    debits = await _count(
        db_sessionmaker,
        "SELECT count(*) FROM ledger_transactions WHERE user_id=:u AND type='debit'",
        uid,
    )
    assert debits == 0


@pytest.mark.asyncio
async def test_byok_runtime_invalid_blocks(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(
            s, subscription="active", balance=0, byok_enabled=True, byok_status="valid"
        )
    fake_anthropic.auth_error_keys = {"sk-ant-user-key"}
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "projectId": "p", "message": "hi", "mode": "byok"},
        headers=auth_headers(uid),
    )
    # Anthropic rejected the BYOK key → business block byok_invalid (HTTP 200).
    assert r.status_code == 200
    assert r.json()["status"] == "blocked"
    assert r.json()["blockReason"] == "byok_invalid"
