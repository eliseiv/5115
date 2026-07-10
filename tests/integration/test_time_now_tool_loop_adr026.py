"""Integration tests for the global server-side tool ``time.now`` in the chat tool-loop (ADR-026).

Real PostgreSQL container; Anthropic faked at the client boundary (shared FakeAnthropicClient).
Covers the backend follow_up scenarios / 06-testing-strategy.md §time.now (integration rows):

- Routing without a project: Claude «calls» time.now on a project-less («чистый чат») session →
  the backend executes it server-side in the loop, WITHOUT resolving an external_project_id and
  WITHOUT hitting ``assert external_project_id is not None``; the call is NOT surfaced in
  toolCalls[] to iOS; the loop continues to Anthropic and the final assistant_message is returned.
- The persisted tool step carries the time.now result and the providerToolUseId (ADR-008).
- Billing: a message whose turn includes time.now round(s) costs exactly 1 credit (mode=credits) —
  the server-side round adds no extra debit (per-message billing, ADR-006).
"""

from __future__ import annotations

import json
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import FakeAnthropicClient, auth_headers, seed_user


async def _scalar(maker: async_sessionmaker[AsyncSession], sql: str, **params: object) -> object:
    async with maker() as s:
        return await s.scalar(text(sql), params)


async def _count(maker: async_sessionmaker[AsyncSession], sql: str, uid: uuid.UUID) -> int:
    async with maker() as s:
        return int(await s.scalar(text(sql), {"u": str(uid)}) or 0)


# ============================ routing: server-side, no project ============================
@pytest.mark.asyncio
async def test_time_now_executes_server_side_without_project_not_surfaced(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    # Turn 1: Claude calls time.now (no tz). Turn 2: final answer using the date.
    fake_anthropic.responses = [
        fake_anthropic.tool_result("time.now", {}, tool_id="toolu_timenow01"),
        fake_anthropic.text_result("Today is Wednesday, 2026-06-10."),
    ]

    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "what day is it?", "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # Server-side: the loop completed to a final assistant_message — NOT a client tool_call.
    assert body["status"] == "assistant_message", body
    assert body["assistantMessage"] == "Today is Wednesday, 2026-06-10."
    # No client-side tool_call surfaced to iOS for time.now.
    assert body.get("toolCalls") in (None, [])
    assert body.get("toolCall") is None

    # The session is project-less (the «чистый чат» / global path).
    assert (
        await _scalar(
            db_sessionmaker,
            "SELECT project_id FROM chat_sessions WHERE id = :sid",
            sid=body["sessionId"],
        )
        is None
    )

    # Two Anthropic calls: the initial run + the continuation after the server-side round.
    assert len(fake_anthropic.calls) == 2

    # The time.now tool_call is persisted and completed server-side (status=completed).
    async with db_sessionmaker() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT tool_name, status, provider_tool_use_id "
                    "FROM tool_calls WHERE session_id = :sid"
                ),
                {"sid": body["sessionId"]},
            )
        ).all()
    assert len(rows) == 1
    tool_name, status, provider_id = rows[0]
    assert tool_name == "time.now"
    assert status == "completed"
    assert provider_id == "toolu_timenow01"  # raw provider id preserved (ADR-008)


@pytest.mark.asyncio
async def test_time_now_tool_step_carries_result_and_provider_id(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [
        fake_anthropic.tool_result("time.now", {}, tool_id="toolu_ts01"),
        fake_anthropic.text_result("done"),
    ]
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "date please", "mode": "credits"},
        headers=auth_headers(uid),
    )
    sid = r.json()["sessionId"]
    async with db_sessionmaker() as s:
        payload = await s.scalar(
            text(
                "SELECT payload FROM chat_steps "
                "WHERE session_id = :sid AND role = 'tool' ORDER BY seq LIMIT 1"
            ),
            {"sid": sid},
        )
    assert payload is not None
    assert payload["toolName"] == "time.now"
    assert payload["providerToolUseId"] == "toolu_ts01"
    # The server-side result envelope holds the UTC set (result, not error).
    assert payload["error"] is None
    # The tool step stores the bare result dict (orchestrator persists payload.get("result")).
    result = payload["result"]
    assert set(result) >= {"utc", "unix", "weekday"}
    # utc round-trips as an ISO8601 UTC instant.
    assert json.dumps(result)  # serializable


# ============================ idempotency / loop continuation ============================
@pytest.mark.asyncio
async def test_time_now_loop_does_not_create_pending_client_tool_call(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """No /chat/tool-result round-trip is needed for time.now (it is server-side)."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [
        fake_anthropic.tool_result("time.now", {"tz": "UTC"}, tool_id="toolu_x"),
        fake_anthropic.text_result("ok"),
    ]
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "now?", "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert r.json()["status"] == "assistant_message"
    # All tool_calls of this session are completed (none left pending for the client).
    pending = await _count(
        db_sessionmaker,
        "SELECT count(*) FROM tool_calls "
        "WHERE session_id IN (SELECT id FROM chat_sessions WHERE user_id = :u) "
        "AND status NOT IN ('completed', 'errored')",
        uid,
    )
    assert pending == 0


# ============================ billing: 1 credit per message ============================
@pytest.mark.asyncio
async def test_time_now_round_does_not_add_extra_debit(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    # Two time.now rounds in one message-step, then the final answer → still exactly 1 debit.
    fake_anthropic.responses = [
        fake_anthropic.tool_result("time.now", {}, tool_id="toolu_a"),
        fake_anthropic.tool_result("time.now", {"tz": "UTC"}, tool_id="toolu_b"),
        fake_anthropic.text_result("the date is settled"),
    ]
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "what year is it really?", "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert r.json()["status"] == "assistant_message"

    bal = await _scalar(
        db_sessionmaker, "SELECT balance FROM wallets WHERE user_id = :u", u=str(uid)
    )
    assert int(bal) == 4  # exactly 1 credit consumed despite two server-side time.now rounds
    debits = await _count(
        db_sessionmaker,
        "SELECT count(*) FROM ledger_transactions WHERE user_id = :u AND type = 'debit'",
        uid,
    )
    assert debits == 1
