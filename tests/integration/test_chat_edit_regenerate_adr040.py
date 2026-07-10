"""Integration tests for ADR-040 (CO-8) — edit message + regenerate via `editMessageStepId`.

Real PostgreSQL container (testcontainers); the LLM client is faked at the create_message
boundary (conftest `FakeAnthropicClient`, the faithful `LLMClient` double — `tool_use.id` is a
realistic `toolu_...`, NOT a UUID). Anthropic is the default provider in tests. Hermetic: no
network, passes with placeholder API keys.

ADR-040 contract under test (POST /v1/chat/run + new optional field `editMessageStepId`):
- §2 truncation by `seq`: anchor = min(seq) of the `role='user'` step with this message_step_id;
  delete that user-step + EVERYTHING after (its assistant/tool steps + all later turns); earlier
  turns preserved. Explicit deletion of `tool_calls` of the truncated turns (FK on chat_sessions,
  NOT chat_steps → no cascade) — no orphans.
- §1/§Schema: 422 when `editMessageStepId` is given without `sessionId` (schema validator).
- §1/§5: 404 message_not_found on a foreign/missing/expired session (resume not performed — the
  just-created empty session is rolled back, the foreign chat untouched); the wire `code` is
  exactly `message_not_found` (NOT the default `not_found`).
- §1/§4в: 404 message_not_found when the message_step_id has no user-step (non-existent in the
  session, or it points only at an assistant/tool step — anchor is strictly `role='user'`).
- §4а: edit of the FIRST message truncates the whole history; the session still exists
  (is_new=False); workspace files NOT re-injected; instructions injected as usual.
- §4б: edit with an OPEN tool-loop (pending tool_calls / unclosed barrier) — steps and their
  tool_calls removed, new turn starts from a clean boundary.
- §3: a new debit on the new message_step_id (uuid4, distinct from the truncated one); NO refund
  for the truncated old turn (no-refund).
- §6: backward compatibility — a request WITHOUT `editMessageStepId` behaves exactly as today.
- §2: atomicity — truncation + the new user-step happen in the one request transaction.

Covers follow_up_for_qa scenarios 1–11.
"""

from __future__ import annotations

import base64
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.chat.repository import ChatRepository
from tests.conftest import FakeAnthropicClient, auth_headers, seed_user

_INSTRUCTIONS = "ALWAYS_REPLY_IN_HAIKU"
_KNOWLEDGE_BLOB = "WORKSPACE_KNOWLEDGE_BLOB_UNIQUE"


# --------------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------------
async def _run(
    client: AsyncClient,
    uid: uuid.UUID,
    fake: FakeAnthropicClient,
    *,
    message: str = "go",
    session_id: str | None = None,
    project_id: str | None = None,
    workspace_id: str | None = None,
    edit_message_step_id: str | None = None,
    text_reply: str = "ok",
) -> dict[str, object]:
    """One `/chat/run` turn yielding an assistant_message; returns the response body."""
    fake.responses = [fake.text_result(text_reply)]
    body: dict[str, object] = {"userId": str(uid), "message": message, "mode": "credits"}
    if session_id is not None:
        body["sessionId"] = session_id
    if project_id is not None:
        body["projectId"] = project_id
    if workspace_id is not None:
        body["workspaceProjectId"] = workspace_id
    if edit_message_step_id is not None:
        body["editMessageStepId"] = edit_message_step_id
    r = await client.post("/v1/chat/run", json=body, headers=auth_headers(uid))
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "assistant_message", out
    return out


async def _create_workspace(
    client: AsyncClient, uid: uuid.UUID, **body: object
) -> dict[str, object]:
    payload: dict[str, object] = {"name": "Proj"}
    payload.update(body)
    r = await client.post("/v1/workspaces", json=payload, headers=auth_headers(uid))
    assert r.status_code == 201, r.text
    return r.json()


async def _add_knowledge_file(
    client: AsyncClient, uid: uuid.UUID, workspace_id: str, blob: str
) -> None:
    r = await client.post(
        f"/v1/workspaces/{workspace_id}/files",
        json={
            "type": "text",
            "mediaType": "text/plain",
            "filename": "notes.txt",
            "data": base64.b64encode(blob.encode()).decode("ascii"),
        },
        headers=auth_headers(uid),
    )
    assert r.status_code == 201, r.text


async def _steps(maker: async_sessionmaker[AsyncSession], session_id: str) -> list[tuple[str, str]]:
    """All (role, message_step_id) of a session ordered by seq."""
    async with maker() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT role, message_step_id FROM chat_steps "
                    "WHERE session_id=:sid ORDER BY seq"
                ),
                {"sid": session_id},
            )
        ).all()
    return [(r[0], str(r[1])) for r in rows]


async def _tool_call_count(maker: async_sessionmaker[AsyncSession], session_id: str) -> int:
    async with maker() as s:
        return int(
            await s.scalar(
                text("SELECT count(*) FROM tool_calls WHERE session_id=:sid"),
                {"sid": session_id},
            )
        )


async def _balance(maker: async_sessionmaker[AsyncSession], uid: uuid.UUID) -> int:
    async with maker() as s:
        return int(
            await s.scalar(text("SELECT balance FROM wallets WHERE user_id=:u"), {"u": str(uid)})
        )


async def _debit_count(maker: async_sessionmaker[AsyncSession], uid: uuid.UUID) -> int:
    async with maker() as s:
        return int(
            await s.scalar(
                text("SELECT count(*) FROM ledger_transactions WHERE user_id=:u AND type='debit'"),
                {"u": str(uid)},
            )
        )


# ==================================================================================================
# Scenario 1 + 11 — truncate by seq (middle turn): edited user-step + its turn + all later turns
#                    removed; earlier turns preserved; truncation + new user-step atomic in one txn.
# ==================================================================================================
@pytest.mark.asyncio
async def test_edit_middle_turn_truncates_by_seq_preserving_earlier(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=10)

    # Three turns: t1, t2, t3. Edit t2 → t2 + t3 dropped, t1 kept, a NEW turn appended.
    t1 = await _run(client, uid, fake_anthropic, message="turn-1", text_reply="a1")
    sid = str(t1["sessionId"])
    msid1 = str(t1["messageStepId"])

    t2 = await _run(client, uid, fake_anthropic, message="turn-2", session_id=sid, text_reply="a2")
    msid2 = str(t2["messageStepId"])

    t3 = await _run(client, uid, fake_anthropic, message="turn-3", session_id=sid, text_reply="a3")
    msid3 = str(t3["messageStepId"])

    # Pre-edit: 6 steps (3 user + 3 assistant), all distinct message_step_ids.
    before = await _steps(db_sessionmaker, sid)
    assert before == [
        ("user", msid1),
        ("assistant", msid1),
        ("user", msid2),
        ("assistant", msid2),
        ("user", msid3),
        ("assistant", msid3),
    ], before

    # Edit turn 2.
    edited = await _run(
        client,
        uid,
        fake_anthropic,
        message="turn-2-edited",
        session_id=sid,
        edit_message_step_id=msid2,
        text_reply="a2-new",
    )
    assert edited["sessionId"] == sid
    new_msid = str(edited["messageStepId"])
    # New turn carries a fresh message_step_id (uuid4), distinct from the truncated turn (§3).
    assert new_msid not in {msid1, msid2, msid3}

    after = await _steps(db_sessionmaker, sid)
    # t1 preserved; t2 + t3 removed; the new edited turn appended (user + assistant).
    assert after == [
        ("user", msid1),
        ("assistant", msid1),
        ("user", new_msid),
        ("assistant", new_msid),
    ], after
    # The new generation actually replaced the answer.
    assert edited["assistantMessage"] == "a2-new"


# ==================================================================================================
# Scenario 2 — explicit tool_calls deletion: no orphaned tool_calls remain for truncated turns.
#              FK of tool_calls is on chat_sessions, NOT chat_steps → cascade would NOT remove them.
# ==================================================================================================
@pytest.mark.asyncio
async def test_edit_deletes_orphan_tool_calls_of_truncated_turns(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=10)

    # Turn 1: client-side tool_call (files.read) → completes → final. A tool_calls row is created.
    fake_anthropic.responses = [
        fake_anthropic.tool_result("files.read", {"path": "a.txt"}),
        fake_anthropic.text_result("done"),
    ]
    r1 = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "use a tool", "mode": "credits"},
        headers=auth_headers(uid),
    )
    b1 = r1.json()
    assert b1["status"] == "tool_call", b1
    sid = str(b1["sessionId"])
    msid1 = str(b1["messageStepId"])
    tcid = b1["toolCall"]["id"]
    r1b = await client.post(
        "/v1/chat/tool-result",
        json={"userId": str(uid), "sessionId": sid, "toolCallId": tcid, "result": {"ok": 1}},
        headers=auth_headers(uid),
    )
    assert r1b.json()["status"] == "assistant_message"

    assert await _tool_call_count(db_sessionmaker, sid) == 1

    # Edit turn 1 (the only turn). Its tool_calls must be explicitly removed (no cascade).
    edited = await _run(
        client,
        uid,
        fake_anthropic,
        message="no tool now",
        session_id=sid,
        edit_message_step_id=msid1,
        text_reply="clean",
    )
    assert edited["assistantMessage"] == "clean"
    # No orphaned tool_calls remain for the truncated turn.
    assert await _tool_call_count(db_sessionmaker, sid) == 0


# ==================================================================================================
# Scenario 3 — 422 on editMessageStepId WITHOUT sessionId (schema validator).
# ==================================================================================================
@pytest.mark.asyncio
async def test_edit_without_session_id_422(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)

    r = await client.post(
        "/v1/chat/run",
        json={
            "userId": str(uid),
            "message": "edit me",
            "mode": "credits",
            "editMessageStepId": str(uuid.uuid4()),
            # NO sessionId.
        },
        headers=auth_headers(uid),
    )
    assert r.status_code == 422, r.text


# ==================================================================================================
# Scenario 4 — 404 message_not_found on a foreign / non-existent / expired session: NO truncation,
#              NO persisted empty new session (rollback), foreign chat untouched; wire code exact.
# ==================================================================================================
@pytest.mark.asyncio
async def test_edit_foreign_session_404_message_not_found_isolation(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        owner = await seed_user(s, subscription="active", balance=5)
        other = await seed_user(s, subscription="active", balance=5)

    # `other` owns a real session with a real turn.
    foreign = await _run(client, other, fake_anthropic, message="theirs", text_reply="x")
    foreign_sid = str(foreign["sessionId"])
    foreign_msid = str(foreign["messageStepId"])
    foreign_steps_before = await _steps(db_sessionmaker, foreign_sid)

    # `owner` edits `other`'s session → resume not performed (foreign) → 404 message_not_found.
    r = await client.post(
        "/v1/chat/run",
        json={
            "userId": str(owner),
            "sessionId": foreign_sid,
            "message": "hijack",
            "mode": "credits",
            "editMessageStepId": foreign_msid,
        },
        headers=auth_headers(owner),
    )
    assert r.status_code == 404, r.text
    assert r.json()["error"]["code"] == "message_not_found", r.text

    # Foreign chat untouched (no truncation across users).
    assert await _steps(db_sessionmaker, foreign_sid) == foreign_steps_before

    # No empty session was persisted for `owner` (the just-created session row rolled back).
    async with db_sessionmaker() as s:
        owner_sessions = int(
            await s.scalar(
                text("SELECT count(*) FROM chat_sessions WHERE user_id=:u"), {"u": str(owner)}
            )
        )
    assert owner_sessions == 0


@pytest.mark.asyncio
async def test_edit_nonexistent_session_404_no_session_persisted(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """A sessionId that does not exist at all → resume creates is_new=True → 404; rolled back."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)

    r = await client.post(
        "/v1/chat/run",
        json={
            "userId": str(uid),
            "sessionId": str(uuid.uuid4()),  # non-existent
            "message": "edit",
            "mode": "credits",
            "editMessageStepId": str(uuid.uuid4()),
        },
        headers=auth_headers(uid),
    )
    assert r.status_code == 404, r.text
    assert r.json()["error"]["code"] == "message_not_found", r.text

    async with db_sessionmaker() as s:
        sessions = int(
            await s.scalar(
                text("SELECT count(*) FROM chat_sessions WHERE user_id=:u"), {"u": str(uid)}
            )
        )
    assert sessions == 0  # the empty just-created session was rolled back


# ==================================================================================================
# Scenario 5 — 404 message_not_found on a non-existent msid in the OWN session (anchor=None).
# ==================================================================================================
@pytest.mark.asyncio
async def test_edit_unknown_message_step_in_own_session_404(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)

    run = await _run(client, uid, fake_anthropic, message="hello", text_reply="hi")
    sid = str(run["sessionId"])
    steps_before = await _steps(db_sessionmaker, sid)

    r = await client.post(
        "/v1/chat/run",
        json={
            "userId": str(uid),
            "sessionId": sid,
            "message": "edit",
            "mode": "credits",
            "editMessageStepId": str(uuid.uuid4()),  # not in this session
        },
        headers=auth_headers(uid),
    )
    assert r.status_code == 404, r.text
    assert r.json()["error"]["code"] == "message_not_found", r.text
    # History untouched (no truncation when anchor not found).
    assert await _steps(db_sessionmaker, sid) == steps_before


# ==================================================================================================
# Scenario 6 — 404 message_not_found when editMessageStepId points at an assistant/tool step only
#              (no user-step) — anchor is strictly role='user'.
# ==================================================================================================
@pytest.mark.asyncio
async def test_edit_message_step_with_no_user_step_404(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """A message_step_id that has assistant/tool steps but NO user step → anchor=None → 404."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
        sid = uuid.uuid4()
        await s.execute(
            text("INSERT INTO chat_sessions (id, user_id, mode) VALUES (:id, :uid, 'credits')"),
            {"id": str(sid), "uid": str(uid)},
        )
        await s.commit()
        repo = ChatRepository(s)
        # A normal user turn (so the session is non-empty).
        msid_user = uuid.uuid4()
        await repo.add_step(
            session_id=sid, message_step_id=msid_user, role="user", payload={"content": []}
        )
        await repo.add_step(
            session_id=sid,
            message_step_id=msid_user,
            role="assistant",
            payload={"content": [{"type": "text", "text": "a"}]},
        )
        # An assistant/tool-only message_step_id (no user step) — e.g. a server-side artifact.
        msid_assistant_only = uuid.uuid4()
        await repo.add_step(
            session_id=sid,
            message_step_id=msid_assistant_only,
            role="assistant",
            payload={"content": [{"type": "text", "text": "b"}]},
        )
        await s.commit()

    r = await client.post(
        "/v1/chat/run",
        json={
            "userId": str(uid),
            "sessionId": str(sid),
            "message": "edit the assistant step?",
            "mode": "credits",
            "editMessageStepId": str(msid_assistant_only),
        },
        headers=auth_headers(uid),
    )
    assert r.status_code == 404, r.text
    assert r.json()["error"]["code"] == "message_not_found", r.text
    # No truncation happened (all 3 steps remain).
    after = await _steps(db_sessionmaker, str(sid))
    assert len(after) == 3, after


# ==================================================================================================
# Scenario 7 — edit of the FIRST message: whole history truncated, session still exists
#              (is_new=False), workspace files NOT re-injected, instructions injected as usual.
# ==================================================================================================
@pytest.mark.asyncio
async def test_edit_first_message_truncates_all_no_file_reinjection(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=10)
    w = await _create_workspace(client, uid, name="X", instructions=_INSTRUCTIONS)
    await _add_knowledge_file(client, uid, str(w["id"]), _KNOWLEDGE_BLOB)

    # turn 0 inside the workspace: instructions in system AND knowledge file in the user turn.
    t1 = await _run(
        client, uid, fake_anthropic, message="first", workspace_id=str(w["id"]), text_reply="a1"
    )
    sid = str(t1["sessionId"])
    msid1 = str(t1["messageStepId"])
    assert _INSTRUCTIONS in fake_anthropic.calls[0]["system_prompt"]
    assert _KNOWLEDGE_BLOB in str(fake_anthropic.calls[0]["messages"])

    # Edit the FIRST message → the whole history is truncated; the session row persists.
    edited = await _run(
        client,
        uid,
        fake_anthropic,
        message="first-edited",
        session_id=sid,
        edit_message_step_id=msid1,
        text_reply="a1-new",
    )
    assert edited["sessionId"] == sid  # same session (is_new=False — not recreated)
    new_msid = str(edited["messageStepId"])
    assert new_msid != msid1

    # Only the new edited turn remains.
    after = await _steps(db_sessionmaker, sid)
    assert after == [("user", new_msid), ("assistant", new_msid)], after

    # instructions injected as usual (decoupled from is_new), but workspace FILES NOT re-injected
    # (turn-0-only, variant a) on the edited (resume) turn.
    last_call = fake_anthropic.calls[-1]
    assert _INSTRUCTIONS in last_call["system_prompt"]
    assert _KNOWLEDGE_BLOB not in str(last_call["messages"])


# ==================================================================================================
# Scenario 8 — edit with an OPEN tool-loop (pending tool_calls / unclosed barrier): the pending
#              steps + their tool_calls removed; the new turn starts from a clean boundary.
# ==================================================================================================
@pytest.mark.asyncio
async def test_edit_with_open_tool_loop_clears_pending(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=10)

    # Turn 1: assistant asks for a client-side tool but the client NEVER returns the result →
    # a pending tool_call + an unclosed barrier remain.
    fake_anthropic.responses = [fake_anthropic.tool_result("files.read", {"path": "a.txt"})]
    r1 = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "open a loop", "mode": "credits"},
        headers=auth_headers(uid),
    )
    b1 = r1.json()
    assert b1["status"] == "tool_call", b1
    sid = str(b1["sessionId"])
    msid1 = str(b1["messageStepId"])

    # A pending tool_call exists; the user step + assistant tool_use step exist.
    assert await _tool_call_count(db_sessionmaker, sid) == 1
    async with db_sessionmaker() as s:
        pending = int(
            await s.scalar(
                text("SELECT count(*) FROM tool_calls WHERE session_id=:sid AND status='pending'"),
                {"sid": sid},
            )
        )
    assert pending == 1

    # Edit the open turn → its steps + pending tool_calls cleared; new turn from a clean boundary.
    edited = await _run(
        client,
        uid,
        fake_anthropic,
        message="restart cleanly",
        session_id=sid,
        edit_message_step_id=msid1,
        text_reply="fresh",
    )
    assert edited["assistantMessage"] == "fresh"
    new_msid = str(edited["messageStepId"])
    assert new_msid != msid1

    # No leftover tool_calls (the pending one was removed with the truncated turn).
    assert await _tool_call_count(db_sessionmaker, sid) == 0
    after = await _steps(db_sessionmaker, sid)
    assert after == [("user", new_msid), ("assistant", new_msid)], after


# ==================================================================================================
# Scenario 9 — new debit on the new message_step_id; NO refund for the truncated old turn.
# ==================================================================================================
@pytest.mark.asyncio
async def test_edit_bills_new_turn_no_refund(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=10)

    # Turn 1: 1 debit.
    t1 = await _run(client, uid, fake_anthropic, message="turn-1", text_reply="a1")
    sid = str(t1["sessionId"])
    msid1 = str(t1["messageStepId"])
    assert await _balance(db_sessionmaker, uid) == 9
    assert await _debit_count(db_sessionmaker, uid) == 1

    # Edit turn 1 → a NEW debit (no-refund for the truncated turn).
    edited = await _run(
        client,
        uid,
        fake_anthropic,
        message="turn-1-edited",
        session_id=sid,
        edit_message_step_id=msid1,
        text_reply="a1-new",
    )
    new_msid = str(edited["messageStepId"])
    assert new_msid != msid1

    # Exactly one MORE debit (new message_step_id); the old turn's credit was NOT refunded.
    assert await _balance(db_sessionmaker, uid) == 8
    assert await _debit_count(db_sessionmaker, uid) == 2

    async with db_sessionmaker() as s:
        # The new debit is keyed by the new message_step_id (idempotency key, ADR-005/006 →
        # idempotency_key == str(message_step_id)).
        new_debits = int(
            await s.scalar(
                text(
                    "SELECT count(*) FROM ledger_transactions "
                    "WHERE user_id=:u AND type='debit' AND idempotency_key=:k"
                ),
                {"u": str(uid), "k": new_msid},
            )
        )
        # No refund/credit transaction was recorded for the truncated turn.
        refunds = int(
            await s.scalar(
                text(
                    "SELECT count(*) FROM ledger_transactions " "WHERE user_id=:u AND type<>'debit'"
                ),
                {"u": str(uid)},
            )
        )
    assert new_debits == 1, "the new turn's debit must be keyed by the new message_step_id"
    assert refunds == 0, "no refund/credit for the truncated old turn (no-refund on edit)"


# ==================================================================================================
# Scenario 10 — backward compatibility: a request WITHOUT editMessageStepId behaves as today.
# ==================================================================================================
@pytest.mark.asyncio
async def test_run_without_edit_field_unchanged(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """No editMessageStepId → normal /chat/run: no truncation, resume appends turns as usual."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=10)

    t1 = await _run(client, uid, fake_anthropic, message="turn-1", text_reply="a1")
    sid = str(t1["sessionId"])
    msid1 = str(t1["messageStepId"])

    t2 = await _run(client, uid, fake_anthropic, message="turn-2", session_id=sid, text_reply="a2")
    msid2 = str(t2["messageStepId"])

    # Both turns are present, in order — no truncation occurred.
    after = await _steps(db_sessionmaker, sid)
    assert after == [
        ("user", msid1),
        ("assistant", msid1),
        ("user", msid2),
        ("assistant", msid2),
    ], after
    assert msid1 != msid2
    # Each turn billed once (2 debits).
    assert await _debit_count(db_sessionmaker, uid) == 2
