"""Integration tests for ADR-021 next_step_after-by-seq + server-side loop normalization.

Real PostgreSQL container; Anthropic faked at the client boundary. Covers:
- scenario 2: multi-round tool-loop in one message-step → next_step_after anchored to seq returns
  the assistant step of the RIGHT round; idempotent replay of /chat/tool-result with the same
  toolCallId does not double-bill or change the answer.
- scenario 3 (persist boundary): after a server-side site.* loop, chat_steps.payload AND the
  messages assembled for Anthropic carry NO non-wire `caller` field; tool_use.id is preserved
  verbatim (ADR-008); text/tool_use content is not lost.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.chat.anthropic_client import AnthropicResult, AnthropicUsage
from app.chat.repository import ChatRepository
from tests.conftest import FakeAnthropicClient, auth_headers, seed_user
from tests.fake_client_tool import FAKE_CLIENT_TOOL, register_fake_client_tool


@pytest.fixture(autouse=True)
def _register_fake_client_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    # ADR-063: register a test-only client-side example tool (no shipped client-side tool remains).
    register_fake_client_tool(monkeypatch)


def _usage() -> AnthropicUsage:
    return AnthropicUsage(
        input_tokens=10,
        output_tokens=5,
        model="claude-sonnet-4-5",
        cache_read_tokens=0,
        cache_write_tokens=0,
    )


def _server_side_tool_use_result(
    wire_name: str, domain_name: str, tool_id: str, args: dict[str, Any]
) -> Any:
    """A tool_use AnthropicResult mirroring the real post-create_message shape.

    In production create_message returns content_blocks holding the RAW Anthropic wire name
    (underscore, e.g. site_write_file) replayed verbatim on continuation, while tool_uses[] carries
    the reverse-mapped DOMAIN name (dotted, e.g. site.write_file) used for routing/tool_calls. The
    blocks are already wire-clean (`caller` stripped by _normalize_block at the persist boundary —
    asserted by the dedicated unit test). Here we assert the orchestrator never RE-introduces
    non-wire fields and preserves the raw id verbatim across persist + replay.
    """
    return AnthropicResult(
        stop_reason="tool_use",
        content_blocks=[{"type": "tool_use", "id": tool_id, "name": wire_name, "input": args}],
        usage=_usage(),
        text="",
        tool_uses=[{"id": tool_id, "name": domain_name, "input": args}],
    )


# --------------------------- scenario 2: next_step_after by seq ---------------------------
@pytest.mark.asyncio
async def test_next_step_after_returns_round_specific_assistant_step(
    db_session: AsyncSession,
) -> None:
    """Multi-round tool-loop in ONE message-step: next_step_after(seq) returns each round's step.

    Layout (one message_step_id, server-side style — all in insertion order via seq):
      seq1 user
      seq2 assistant(tool_use #1)
      seq3 tool(tool_result #1)        <- anchor A (toolCallId tc1)
      seq4 assistant(text "round1")    <- next_step_after(tc1) MUST return THIS
      seq5 assistant(tool_use #2)
      seq6 tool(tool_result #2)        <- anchor B (toolCallId tc2)
      seq7 assistant(text "round2")    <- next_step_after(tc2) MUST return THIS
    """
    uid = await seed_user(db_session, balance=0)
    sid = uuid.uuid4()
    await db_session.execute(
        text(
            "INSERT INTO chat_sessions (id, user_id, project_id, mode) "
            "VALUES (:id, :uid, 'p', 'credits')"
        ),
        {"id": str(sid), "uid": str(uid)},
    )
    repo = ChatRepository(db_session)
    msid = uuid.uuid4()
    tc1 = uuid.uuid4()
    tc2 = uuid.uuid4()

    await repo.add_step(session_id=sid, message_step_id=msid, role="user", payload={"content": []})
    await repo.add_step(
        session_id=sid,
        message_step_id=msid,
        role="assistant",
        payload={
            "content": [{"type": "tool_use", "id": "toolu_1", "name": "site_list", "input": {}}]
        },
    )
    await repo.add_step(
        session_id=sid,
        message_step_id=msid,
        role="tool",
        payload={"toolCallId": str(tc1), "providerToolUseId": "toolu_1", "result": {}},
    )
    round1 = await repo.add_step(
        session_id=sid,
        message_step_id=msid,
        role="assistant",
        payload={"content": [{"type": "text", "text": "round1"}]},
    )
    await repo.add_step(
        session_id=sid,
        message_step_id=msid,
        role="assistant",
        payload={
            "content": [{"type": "tool_use", "id": "toolu_2", "name": "site_list", "input": {}}]
        },
    )
    await repo.add_step(
        session_id=sid,
        message_step_id=msid,
        role="tool",
        payload={"toolCallId": str(tc2), "providerToolUseId": "toolu_2", "result": {}},
    )
    round2 = await repo.add_step(
        session_id=sid,
        message_step_id=msid,
        role="assistant",
        payload={"content": [{"type": "text", "text": "round2"}]},
    )
    await db_session.flush()

    got1 = await repo.next_step_after(sid, msid, tc1)
    got2 = await repo.next_step_after(sid, msid, tc2)
    assert got1 is not None and got1.id == round1.id
    assert got1.payload["content"][0]["text"] == "round1"
    assert got2 is not None and got2.id == round2.id
    assert got2.payload["content"][0]["text"] == "round2"
    # seq ordering invariant: anchor < returned assistant step.
    assert got1.seq > 0 and got2.seq > got1.seq


@pytest.mark.asyncio
async def test_next_step_after_falls_back_to_latest_when_no_anchor(
    db_session: AsyncSession,
) -> None:
    """No tool step matches the toolCallId → fall back to the latest assistant step (max seq)."""
    uid = await seed_user(db_session, balance=0)
    sid = uuid.uuid4()
    await db_session.execute(
        text(
            "INSERT INTO chat_sessions (id, user_id, project_id, mode) "
            "VALUES (:id, :uid, 'p', 'credits')"
        ),
        {"id": str(sid), "uid": str(uid)},
    )
    repo = ChatRepository(db_session)
    msid = uuid.uuid4()
    await repo.add_step(
        session_id=sid,
        message_step_id=msid,
        role="assistant",
        payload={"content": [{"type": "text", "text": "first"}]},
    )
    latest = await repo.add_step(
        session_id=sid,
        message_step_id=msid,
        role="assistant",
        payload={"content": [{"type": "text", "text": "latest"}]},
    )
    await db_session.flush()
    got = await repo.next_step_after(sid, msid, uuid.uuid4())  # unknown toolCallId
    assert got is not None and got.id == latest.id


# --------- scenario 2 (end-to-end HTTP): idempotent replay does not double-bill ---------
@pytest.mark.asyncio
async def test_tool_result_replay_idempotent_no_double_billing(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    # run -> client-side tool_call ; tool-result -> assistant_message (final, billed once).
    fake_anthropic.responses = [
        fake_anthropic.tool_result(FAKE_CLIENT_TOOL, {"path": "a.txt"}),
        fake_anthropic.text_result("the answer"),
    ]
    r1 = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "projectId": "p", "message": "go", "mode": "credits"},
        headers=auth_headers(uid),
    )
    b1 = r1.json()
    assert b1["status"] == "tool_call"
    sess = b1["sessionId"]
    tcid = b1["toolCall"]["id"]

    r2 = await client.post(
        "/v1/chat/tool-result",
        json={"userId": str(uid), "sessionId": sess, "toolCallId": tcid, "result": {"ok": 1}},
        headers=auth_headers(uid),
    )
    assert r2.json()["status"] == "assistant_message"
    assert r2.json()["assistantMessage"] == "the answer"

    # Replay the SAME tool-result (idempotent): same answer, NO extra Anthropic call, NO 2nd debit.
    calls_before = len(fake_anthropic.calls)
    r3 = await client.post(
        "/v1/chat/tool-result",
        json={"userId": str(uid), "sessionId": sess, "toolCallId": tcid, "result": {"ok": 1}},
        headers=auth_headers(uid),
    )
    assert r3.json()["status"] == "assistant_message"
    assert r3.json()["assistantMessage"] == "the answer"  # same round's answer
    assert len(fake_anthropic.calls) == calls_before  # no re-generation on replay

    async with db_sessionmaker() as s:
        bal = await s.scalar(text("SELECT balance FROM wallets WHERE user_id=:u"), {"u": str(uid)})
        debits = await s.scalar(
            text("SELECT count(*) FROM ledger_transactions WHERE user_id=:u AND type='debit'"),
            {"u": str(uid)},
        )
    assert int(bal) == 4  # exactly one credit consumed for the message-step
    assert int(debits) == 1


# --------- scenario 3 (persist boundary): server-side loop payload + replay wire-clean ---------
@pytest.mark.asyncio
async def test_server_side_loop_payload_and_messages_have_no_caller(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """A server-side site.* tool-loop: stored payload + replayed messages carry no `caller`.

    Drives the real orchestrator server-side branch (site.list executed on the backend in one
    transaction with the tool_result — the exact BUG-5 shape). Asserts:
    - chat_steps.payload for the assistant tool_use step has NO `caller` and a verbatim tool_use.id;
    - the continuation messages assembled for Anthropic (2nd call) order tool_use then tool_result
      (seq order) and contain no `caller`;
    - text/tool_use content survives.
    """
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)

    raw_tool_id = "toolu_01ServerSideABC234567"
    fake_anthropic.responses = [
        _server_side_tool_use_result("site_list", "site.list", raw_tool_id, {}),
        fake_anthropic.text_result("listed the files"),
    ]

    r = await client.post(
        "/v1/chat/run",
        json={
            "userId": str(uid),
            "projectId": "proj-x",
            "message": "list files",
            "mode": "credits",
        },
        headers=auth_headers(uid),
    )
    body = r.json()
    # site.list is server-side → no hand-off to iOS; the loop finalizes to assistant_message.
    assert body["status"] == "assistant_message", body
    assert body["assistantMessage"] == "listed the files"

    sess = body["sessionId"]
    async with db_sessionmaker() as s:
        rows = (
            await s.execute(
                text("SELECT role, payload FROM chat_steps WHERE session_id=:sid ORDER BY seq"),
                {"sid": sess},
            )
        ).all()
    roles = [row[0] for row in rows]
    # user -> assistant(tool_use) -> tool(tool_result) -> assistant(final), in seq order.
    assert roles == ["user", "assistant", "tool", "assistant"], roles

    # The assistant tool_use step payload: no `caller`, verbatim raw id (ADR-008).
    tool_use_payload = rows[1][1]
    serialized = json.dumps(tool_use_payload)
    assert "caller" not in serialized
    tool_use_block = tool_use_payload["content"][0]
    assert tool_use_block["type"] == "tool_use"
    assert tool_use_block["id"] == raw_tool_id  # verbatim
    assert tool_use_block["name"] == "site_list"  # anthropic wire name on the assistant turn

    # The continuation (2nd) Anthropic call: tool_use BEFORE tool_result, no `caller` anywhere.
    continuation_messages = fake_anthropic.calls[-1]["messages"]
    blob = json.dumps(continuation_messages)
    assert "caller" not in blob
    # find the assistant tool_use message and the user tool_result message, assert order by index.
    idx_tool_use = next(
        i
        for i, m in enumerate(continuation_messages)
        if isinstance(m.get("content"), list)
        and any(isinstance(b, dict) and b.get("type") == "tool_use" for b in m["content"])
    )
    idx_tool_result = next(
        i
        for i, m in enumerate(continuation_messages)
        if isinstance(m.get("content"), list)
        and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in m["content"])
    )
    assert idx_tool_use < idx_tool_result, "tool_use must precede its tool_result (no orphan)"
    # tool_result.tool_use_id must equal the raw provider id (ADR-008).
    tr_block = next(
        b
        for m in continuation_messages
        if isinstance(m.get("content"), list)
        for b in m["content"]
        if isinstance(b, dict) and b.get("type") == "tool_result"
    )
    assert tr_block["tool_use_id"] == raw_tool_id


@pytest.mark.asyncio
async def test_normalized_payload_persisted_via_create_message_path(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """End-to-end: a plain assistant_message stored payload is wire-clean (no `caller`)."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [fake_anthropic.text_result("hi there")]
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "projectId": "p", "message": "hello", "mode": "credits"},
        headers=auth_headers(uid),
    )
    sess = r.json()["sessionId"]
    async with db_sessionmaker() as s:
        payloads = (
            await s.execute(
                text("SELECT payload FROM chat_steps WHERE session_id=:sid AND role='assistant'"),
                {"sid": sess},
            )
        ).all()
    assert payloads
    assert "caller" not in json.dumps([p[0] for p in payloads])
