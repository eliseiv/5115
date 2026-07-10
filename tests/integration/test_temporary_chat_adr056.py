"""Integration tests for ADR-056 — temporary chat (no persistence, client-supplied history).

Real PostgreSQL container; the active provider (anthropic in the test env) is faked at the client
boundary via the shared FakeAnthropicClient. Normative coverage of the ADR-056 invariants:

- **No-persist**: a ``temporary=true`` ``/chat/run`` writes NOTHING to ``chat_sessions`` /
  ``chat_steps`` / ``tool_calls`` and the conversation never shows in ``GET /v1/chats`` — verified
  by direct SQL counts.
- **Billing**: the turn is still billed (temporary ≠ free) — a ``ledger_transactions`` debit row is
  created, keyed by the response's ``messageStepId``; the billing-debit audit carries ``session_id``
  NULL (no FK to the synthetic, non-persisted session). Idempotent within the one HTTP request.
- **Trial**: a subscription-less user with ``trial_used=false`` has the lifetime trial consumed —
  ``users.trial_used`` flips to TRUE (the non-obvious anti-abuse invariant, ADR-056 §4).
- **Tools**: the offered tool-set drops ``files.*`` / ``calendar.*`` / ``reminders.*`` (client-side)
  but keeps the global server-side ``time.now`` — asserted on the fake client's recorded args.
- **Anomaly**: a client-side ``tool_use`` returned while ``temporary=true`` → 502 (UpstreamError),
  never a hand-off to the client.
- **Response**: ``messageStepId`` non-null, ``stepId`` null, ``sessionId`` a valid UUID.
- **/chat/tool-result** with the synthetic ``sessionId`` → 404 (not resolvable in the DB).
- **Resume impossible**: replaying the synthetic ``sessionId`` WITHOUT ``temporary`` mints a NEW
  persistent session (the id is ignored), not a 500.
- **history replay**: the client transcript reaches the model — asserted on the recorded wire
  messages for the active provider (anthropic).
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.chat.tools import (
    ALL_TOOL_NAMES,
    GLOBAL_SERVER_SIDE_TOOLS,
    SERVER_SIDE_TOOLS,
    to_domain_tool_name,
)
from tests.conftest import FakeAnthropicClient, auth_headers, seed_user

# Client-side tools = everything that is neither project-scoped site.* nor the global time.now.
_CLIENT_SIDE_TOOLS = ALL_TOOL_NAMES - SERVER_SIDE_TOOLS - GLOBAL_SERVER_SIDE_TOOLS


def _offered_domain_tools(call: dict) -> set[str]:
    """Domain (dotted) names of the tools offered to the provider on a recorded create_message."""
    return {to_domain_tool_name(t["name"]) for t in call["tools"]}


async def _scalar(maker: async_sessionmaker[AsyncSession], sql: str, params: dict) -> object:
    async with maker() as s:
        return await s.scalar(text(sql), params)


async def _count(maker: async_sessionmaker[AsyncSession], sql: str, params: dict) -> int:
    return int(await _scalar(maker, sql, params) or 0)  # type: ignore[arg-type]


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except (ValueError, TypeError, AttributeError):
        return False


# ============================ no-persist invariant ============================
@pytest.mark.asyncio
async def test_temporary_run_persists_no_chat_rows_and_not_in_list(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [fake_anthropic.text_result("ephemeral reply")]

    r = await client.post(
        "/v1/chat/run",
        json={
            "userId": str(uid),
            "message": "hello temporarily",
            "mode": "credits",
            "temporary": True,
            "history": [],
        },
        headers=auth_headers(uid),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "assistant_message", body
    assert body["assistantMessage"] == "ephemeral reply"

    # Direct SQL: not a single chat-* row was written for this user.
    sessions = await _count(
        db_sessionmaker, "SELECT count(*) FROM chat_sessions WHERE user_id=:u", {"u": str(uid)}
    )
    steps = await _count(db_sessionmaker, "SELECT count(*) FROM chat_steps", {})
    tool_calls = await _count(db_sessionmaker, "SELECT count(*) FROM tool_calls", {})
    assert sessions == 0
    assert steps == 0
    assert tool_calls == 0

    # And the conversation is not visible in the chat list.
    lst = await client.get("/v1/chats", headers=auth_headers(uid))
    assert lst.status_code == 200, lst.text
    assert lst.json()["items"] == []


# ============================ response shape ============================
@pytest.mark.asyncio
async def test_temporary_response_has_null_step_id_nonnull_message_step_and_uuid_session(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [fake_anthropic.text_result("ok")]

    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "hi", "mode": "credits", "temporary": True},
        headers=auth_headers(uid),
    )
    body = r.json()
    assert body["status"] == "assistant_message", body
    # messageStepId present (billing idempotency key minted per turn); stepId null (no persisted
    # step); sessionId is a valid (synthetic) UUID.
    assert body["messageStepId"] is not None
    assert "stepId" in body and body["stepId"] is None
    assert _is_uuid(body["sessionId"])


# ============================ billing ============================
@pytest.mark.asyncio
async def test_temporary_turn_is_billed_debit_keyed_by_message_step_id_session_null(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [fake_anthropic.text_result("billed")]

    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "charge me", "mode": "credits", "temporary": True},
        headers=auth_headers(uid),
    )
    body = r.json()
    assert body["status"] == "assistant_message", body
    msid = body["messageStepId"]

    # Exactly one debit, keyed by the response's messageStepId (ADR-006 idempotency key).
    debits = await _count(
        db_sessionmaker,
        "SELECT count(*) FROM ledger_transactions WHERE user_id=:u AND type='debit'",
        {"u": str(uid)},
    )
    assert debits == 1
    by_key = await _count(
        db_sessionmaker,
        "SELECT count(*) FROM ledger_transactions "
        "WHERE user_id=:u AND type='debit' AND idempotency_key=:k",
        {"u": str(uid), "k": msid},
    )
    assert by_key == 1

    # Balance was actually decremented (5 → 4).
    balance = await _scalar(
        db_sessionmaker, "SELECT balance FROM wallets WHERE user_id=:u", {"u": str(uid)}
    )
    assert int(balance) == 4  # type: ignore[arg-type]

    # The billing-debit audit carries session_id NULL — no FK to the synthetic (non-persisted)
    # session; the synthetic sessionId is never written as an FK anywhere.
    debit_audits = await _count(
        db_sessionmaker,
        "SELECT count(*) FROM audit_logs WHERE user_id=:u AND event_type='billing_debit'",
        {"u": str(uid)},
    )
    assert debit_audits == 1
    non_null_session_audits = await _count(
        db_sessionmaker,
        "SELECT count(*) FROM audit_logs "
        "WHERE user_id=:u AND event_type='billing_debit' AND session_id IS NOT NULL",
        {"u": str(uid)},
    )
    assert non_null_session_audits == 0


# ============================ trial ============================
@pytest.mark.asyncio
async def test_temporary_turn_consumes_lifetime_trial(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    # No subscription + trial_used=false + mode=credits → the free lifetime trial is used. The
    # non-obvious ADR-056 §4 invariant: a temporary chat still burns the trial (else it would be an
    # unlimited free bypass). users.trial_used must flip to TRUE (a real UPDATE users).
    async with db_sessionmaker() as s:
        uid = await seed_user(s, trial_used=False)
    fake_anthropic.responses = [fake_anthropic.text_result("trial reply")]

    before = await _scalar(
        db_sessionmaker, "SELECT trial_used FROM users WHERE id=:u", {"u": str(uid)}
    )
    assert before is False

    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "first ever", "mode": "credits", "temporary": True},
        headers=auth_headers(uid),
    )
    body = r.json()
    assert body["status"] == "assistant_message", body

    after = await _scalar(
        db_sessionmaker, "SELECT trial_used FROM users WHERE id=:u", {"u": str(uid)}
    )
    assert after is True

    # Trial path has NO debit (ADR-002): the trial flip is the "payment", not a credit.
    debits = await _count(
        db_sessionmaker,
        "SELECT count(*) FROM ledger_transactions WHERE user_id=:u AND type='debit'",
        {"u": str(uid)},
    )
    assert debits == 0
    # Still no chat-* persistence for the temporary trial turn.
    sessions = await _count(
        db_sessionmaker, "SELECT count(*) FROM chat_sessions WHERE user_id=:u", {"u": str(uid)}
    )
    assert sessions == 0


# ============================ tools gating ============================
@pytest.mark.asyncio
async def test_temporary_run_drops_client_side_tools_keeps_time_now(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [fake_anthropic.text_result("ok")]

    await client.post(
        "/v1/chat/run",
        json={
            "userId": str(uid),
            "message": "what time is it",
            "mode": "credits",
            "temporary": True,
        },
        headers=auth_headers(uid),
    )
    offered = _offered_domain_tools(fake_anthropic.calls[0])
    # No client-side tools offered (files.*/calendar.*/reminders.*).
    assert offered.isdisjoint(_CLIENT_SIDE_TOOLS)
    assert not any(n.startswith(("files.", "calendar.", "reminders.")) for n in offered)
    # The global server-side time.now stays offered (executed in-request).
    assert "time.now" in offered
    # No project → project-scoped site.* not offered either.
    assert offered.isdisjoint(SERVER_SIDE_TOOLS)


# ============================ anomaly: client-side tool_use → 502 ============================
@pytest.mark.asyncio
async def test_temporary_client_side_tool_use_is_502_upstream_error(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    # Anomalous: the model returns a client-side files.read tool_use although client-side tools were
    # NOT offered in a temporary chat. It cannot be handed off (no continuation) → UpstreamError.
    fake_anthropic.responses = [
        fake_anthropic.tool_result("files.read", {"path": "a.txt"}, tool_id="toolu_rogueTemp"),
    ]

    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "read a file", "mode": "credits", "temporary": True},
        headers=auth_headers(uid),
    )
    assert r.status_code == 502, r.text
    # Nothing persisted, nothing billed (rolled back on the upstream error).
    tool_calls = await _count(db_sessionmaker, "SELECT count(*) FROM tool_calls", {})
    steps = await _count(db_sessionmaker, "SELECT count(*) FROM chat_steps", {})
    debits = await _count(
        db_sessionmaker,
        "SELECT count(*) FROM ledger_transactions WHERE user_id=:u AND type='debit'",
        {"u": str(uid)},
    )
    assert tool_calls == 0
    assert steps == 0
    assert debits == 0


# ============ /chat/tool-result with synthetic session → 404 ============
@pytest.mark.asyncio
async def test_tool_result_with_synthetic_session_id_is_404(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [fake_anthropic.text_result("ok")]

    run = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "hi", "mode": "credits", "temporary": True},
        headers=auth_headers(uid),
    )
    synthetic_session = run.json()["sessionId"]

    # Continuation is impossible: the synthetic session is not in the DB → 404 (session/tool_call
    # not found), never a 500.
    r = await client.post(
        "/v1/chat/tool-result",
        json={
            "userId": str(uid),
            "sessionId": synthetic_session,
            "toolCallId": str(uuid.uuid4()),
            "result": {"ok": 1},
        },
        headers=auth_headers(uid),
    )
    assert r.status_code == 404, r.text


# ============ resume impossible → new persistent session ============
@pytest.mark.asyncio
async def test_synthetic_session_id_without_temporary_creates_new_persistent_session(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [
        fake_anthropic.text_result("ephemeral"),
        fake_anthropic.text_result("persistent"),
    ]

    run = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "temp turn", "mode": "credits", "temporary": True},
        headers=auth_headers(uid),
    )
    synthetic_session = run.json()["sessionId"]
    # The synthetic session was NOT persisted.
    assert (
        await _count(
            db_sessionmaker,
            "SELECT count(*) FROM chat_sessions WHERE id=:sid",
            {"sid": synthetic_session},
        )
        == 0
    )

    # Replay that id WITHOUT temporary: an unknown sessionId → a NEW persistent session is created
    # (the id is ignored), not a 500 and not a resume.
    r = await client.post(
        "/v1/chat/run",
        json={
            "userId": str(uid),
            "sessionId": synthetic_session,
            "message": "now persistent",
            "mode": "credits",
        },
        headers=auth_headers(uid),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "assistant_message", body
    new_session = body["sessionId"]
    # A brand-new persistent session with a DIFFERENT id was created.
    assert new_session != synthetic_session
    assert (
        await _count(
            db_sessionmaker,
            "SELECT count(*) FROM chat_sessions WHERE id=:sid",
            {"sid": new_session},
        )
        == 1
    )
    # The persistent turn IS visible in the chat list; the synthetic one still is not.
    items = (await client.get("/v1/chats", headers=auth_headers(uid))).json()["items"]
    ids = {it["id"] for it in items}
    assert new_session in ids
    assert synthetic_session not in ids


# ============================ history replay reaches the model ============================
@pytest.mark.asyncio
async def test_history_transcript_is_replayed_to_the_model(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    # The client-supplied transcript must be seeded into the messages sent to the active provider
    # (anthropic in the test env). Distinctive markers per turn are asserted on the recorded wire
    # messages, plus the current message.
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [fake_anthropic.text_result("ok")]

    r = await client.post(
        "/v1/chat/run",
        json={
            "userId": str(uid),
            "message": "CURRENT_TURN_MARKER",
            "mode": "credits",
            "temporary": True,
            "history": [
                {"role": "user", "content": "PRIOR_USER_MARKER"},
                {"role": "assistant", "content": "PRIOR_ASSISTANT_MARKER"},
            ],
        },
        headers=auth_headers(uid),
    )
    assert r.status_code == 200, r.text

    # Flatten the recorded wire messages into text and assert every marker reached the model.
    wire_text = _flatten_wire_text(fake_anthropic.calls[0]["messages"])
    assert "PRIOR_USER_MARKER" in wire_text
    assert "PRIOR_ASSISTANT_MARKER" in wire_text
    assert "CURRENT_TURN_MARKER" in wire_text
    # The transcript precedes the current turn (seed steps are inserted before the new user step).
    assert wire_text.index("PRIOR_USER_MARKER") < wire_text.index("CURRENT_TURN_MARKER")
    assert wire_text.index("PRIOR_ASSISTANT_MARKER") < wire_text.index("CURRENT_TURN_MARKER")


def _flatten_wire_text(messages: list[dict]) -> str:
    """Concatenate all text found in Anthropic wire messages (content blocks or plain strings)."""
    out: list[str] = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            out.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    out.append(str(block.get("text", "")))
                elif isinstance(block, str):
                    out.append(block)
    return "\n".join(out)
