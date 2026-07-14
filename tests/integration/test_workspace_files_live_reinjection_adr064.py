"""Integration tests for ADR-064 — workspace knowledge FILES are LIVE per-turn context.

Regression coverage for a confirmed PROD bug: workspace knowledge files were visible to the model
only on turn 0; on every later turn of the same session the model lost the knowledge base and
hallucinated. ADR-064 (variant A) re-injects the files (assembled via `context_for_session`) into
the FIRST LLM call of EVERY generation request of a workspace session — `/chat/run` turn 0, a
`/chat/run` resume (is_new=False), and the first `/chat/tool-result` continuation — while NEVER
persisting them (base64/`[Файл проекта:]` blocks must not leak into user-facing history — ADR-020 /
ADR-042).

Pattern (06-testing-strategy.md): real PostgreSQL container (testcontainers); the LLM client is
faked at the `create_message` boundary (`FakeAnthropicClient`, the faithful `LLMClient` double that
records the WIRE `messages`/`attachments` the production client would send). We assert on the
CONTENT handed to `create_message` (the injected file block), not merely the reply text. Hermetic:
no network; passes with placeholder/empty API keys (Anthropic is the default provider in tests).

Covers ADR-064 §Указания backend p.7 and the CU cross-turn-persistence/replay rule (multi-turn:
turn ≥2 depends on file content ABSENT from every prior session message and NOT persisted to the
store — proven by a SQL assertion that the store carries 0 occurrences of the fact token).
"""

from __future__ import annotations

import base64
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import FakeAnthropicClient, auth_headers, seed_user
from tests.fake_client_tool import FAKE_CLIENT_TOOL, register_fake_client_tool

# A text knowledge file carrying two DISTINCT facts. Fact A is asked on turn 0; fact B is asked on a
# LATER turn — its token appears ONLY inside the file, never in any user/assistant message, so a
# turn-≥2 request that carries it can only have gotten it from the live re-injected file (the bug
# would drop it, since files are never persisted to the replayed history).
_FACT_A_TOKEN = "OSLO_FACT_A_UNIQUE"
_FACT_B_TOKEN = "OTTER_FACT_B_UNIQUE"
_FILE_TEXT = f"Capital fact: {_FACT_A_TOKEN}. National animal fact: {_FACT_B_TOKEN}."
_FILE_NAME = "kb.txt"
_FILE_LABEL = "[Файл проекта: kb.txt]"
_INSTRUCTIONS = "ALWAYS_REPLY_IN_HAIKU"

# A minimal PNG for the image-file leak check (case 5). Re-encoded verbatim by the service.
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture(autouse=True)
def _register_fake_client_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    # ADR-063: register a test-only client-side example tool (used to reach a client tool_call).
    register_fake_client_tool(monkeypatch)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


async def _create_workspace(
    client: AsyncClient, uid: uuid.UUID, **body: object
) -> dict[str, object]:
    payload: dict[str, object] = {"name": "Proj"}
    payload.update(body)
    r = await client.post("/v1/workspaces", json=payload, headers=auth_headers(uid))
    assert r.status_code == 201, r.text
    return r.json()


async def _add_text_file(
    client: AsyncClient, uid: uuid.UUID, wid: str, *, content: str, filename: str = _FILE_NAME
) -> None:
    r = await client.post(
        f"/v1/workspaces/{wid}/files",
        json={
            "type": "text",
            "mediaType": "text/plain",
            "filename": filename,
            "data": _b64(content.encode()),
        },
        headers=auth_headers(uid),
    )
    assert r.status_code == 201, r.text


async def _add_image_file(
    client: AsyncClient, uid: uuid.UUID, wid: str, *, data: bytes, filename: str = "pic.png"
) -> None:
    r = await client.post(
        f"/v1/workspaces/{wid}/files",
        json={
            "type": "image",
            "mediaType": "image/png",
            "filename": filename,
            "data": _b64(data),
        },
        headers=auth_headers(uid),
    )
    assert r.status_code == 201, r.text


async def _run(
    client: AsyncClient,
    uid: uuid.UUID,
    fake: FakeAnthropicClient,
    *,
    message: str,
    session_id: str | None = None,
    workspace_id: str | None = None,
    edit_message_step_id: str | None = None,
    text_reply: str = "ok",
    responses: list[object] | None = None,
) -> dict[str, object]:
    """One `/chat/run` turn. `responses` (if given) scripts the fake; else a single text reply."""
    fake.responses = responses if responses is not None else [fake.text_result(text_reply)]
    body: dict[str, object] = {"userId": str(uid), "message": message, "mode": "credits"}
    if session_id is not None:
        body["sessionId"] = session_id
    if workspace_id is not None:
        body["workspaceProjectId"] = workspace_id
    if edit_message_step_id is not None:
        body["editMessageStepId"] = edit_message_step_id
    r = await client.post("/v1/chat/run", json=body, headers=auth_headers(uid))
    assert r.status_code == 200, r.text
    return r.json()


async def _session_count(maker: async_sessionmaker[AsyncSession], uid: uuid.UUID) -> int:
    async with maker() as s:
        return int(
            await s.scalar(
                text("SELECT count(*) FROM chat_sessions WHERE user_id=:u"), {"u": str(uid)}
            )
            or 0
        )


async def _user_step_count(maker: async_sessionmaker[AsyncSession], sid: str) -> int:
    async with maker() as s:
        return int(
            await s.scalar(
                text("SELECT count(*) FROM chat_steps WHERE session_id=:s AND role='user'"),
                {"s": sid},
            )
            or 0
        )


async def _store_contains(maker: async_sessionmaker[AsyncSession], sid: str, needle: str) -> bool:
    """True when any persisted chat_steps.payload of the session contains `needle` (leak probe)."""
    async with maker() as s:
        rows = (
            await s.execute(
                text("SELECT payload::text FROM chat_steps WHERE session_id=:s"), {"s": sid}
            )
        ).all()
    return any(needle in r[0] for r in rows)


# ==================================================================================================
# Case 1 — MAIN multi-turn regression: turn 1 (resume, is_new=False) still sees the file (fact B)
# ==================================================================================================
@pytest.mark.asyncio
async def test_file_reinjected_on_run_resume_multiturn(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """turn 0 asks fact A; turn 1 (SAME session, resume) asks fact B — the file rides turn 1 too.

    This is the direct regression of the prod bug (files vanished after turn 0). The fact-B token
    lives ONLY in the file; it is asserted present in the turn-1 `create_message` exactly once and
    NOT present in the persisted store (files are never persisted → the model can only see B via the
    LIVE re-injection on turn 1, not via replayed history).
    """
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=10)
    w = await _create_workspace(client, uid, name="X")
    await _add_text_file(client, uid, str(w["id"]), content=_FILE_TEXT)

    # turn 0 (new session): the file is injected on turn 0 (pre-fix behaviour, unchanged).
    t0 = await _run(
        client, uid, fake_anthropic, message="what is the capital?", workspace_id=str(w["id"])
    )
    sid = str(t0["sessionId"])
    assert str(fake_anthropic.calls[0]["messages"]).count(_FILE_LABEL) == 1

    # turn 1 (RESUME of the same session, is_new=False): ask about fact B — absent from prior msgs.
    t1 = await _run(
        client, uid, fake_anthropic, message="what is the national animal?", session_id=sid
    )
    assert str(t1["sessionId"]) == sid  # same session, not recreated

    # Proof of resume (multi-turn on one session): one session, two user steps appended.
    assert await _session_count(db_sessionmaker, uid) == 1
    assert await _user_step_count(db_sessionmaker, sid) == 2

    # THE FIX: the file block (and fact B) rides the turn-1 create_message exactly once — assembled
    # live from the current workspace_files, injected into the last (turn-1) user turn.
    turn1_messages = str(fake_anthropic.calls[-1]["messages"])
    assert turn1_messages.count(_FILE_LABEL) == 1
    assert _FACT_B_TOKEN in turn1_messages

    # CU cross-turn-replay invariant: fact B reached the model via LIVE re-injection, NOT replay —
    # the store carries ZERO occurrences of the fact token (files are never persisted).
    assert not await _store_contains(db_sessionmaker, sid, _FACT_B_TOKEN)
    assert not await _store_contains(db_sessionmaker, sid, _FILE_LABEL)


# ==================================================================================================
# Case 2 — continuation via /chat/tool-result: file on the FIRST continuation call, not on the
#          internal server-tool round (turn0_attachments consumed after the first call)
# ==================================================================================================
@pytest.mark.asyncio
async def test_file_reinjected_on_tool_result_first_call_only(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """`/chat/tool-result` continuation carries the file on its FIRST create_message; a subsequent
    internal server-tool (time.now) round of the SAME request does NOT repeat it (cost parity)."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=10)
    w = await _create_workspace(client, uid, name="X")
    await _add_text_file(client, uid, str(w["id"]), content=_FILE_TEXT)

    # turn 0: script a CLIENT-side tool_call so the turn parks awaiting /chat/tool-result.
    t0 = await _run(
        client,
        uid,
        fake_anthropic,
        message="go",
        workspace_id=str(w["id"]),
        responses=[fake_anthropic.tool_result(FAKE_CLIENT_TOOL, {"path": "a.txt"})],
    )
    assert t0["status"] == "tool_call", t0
    sid = t0["sessionId"]
    tcid = t0["toolCall"]["id"]
    assert str(fake_anthropic.calls[0]["messages"]).count(_FILE_LABEL) == 1  # turn 0 file

    # /chat/tool-result continuation: LLM first "calls" the global server tool time.now, the backend
    # executes it in-loop, then the LLM returns the final text. → TWO continuation create_message
    # calls in one request.
    fake_anthropic.responses = [
        fake_anthropic.tool_result("time.now", {}, tool_id="toolu_tn01"),
        fake_anthropic.text_result("done"),
    ]
    r = await client.post(
        "/v1/chat/tool-result",
        json={"userId": str(uid), "sessionId": sid, "toolCallId": tcid, "result": {"ok": 1}},
        headers=auth_headers(uid),
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "assistant_message", r.text

    # calls: [0]=turn0, [1]=first continuation call (file present), [2]=server-tool round (no file).
    assert len(fake_anthropic.calls) == 3, fake_anthropic.calls
    first_continuation = str(fake_anthropic.calls[1]["messages"])
    server_tool_round = str(fake_anthropic.calls[2]["messages"])
    assert first_continuation.count(_FILE_LABEL) == 1
    assert _FACT_B_TOKEN in first_continuation
    assert server_tool_round.count(_FILE_LABEL) == 0  # consumed after the first call (cost parity)


# ==================================================================================================
# Case 3 — moved chat (Q-038-1): plain chat → PATCH into a workspace with a file → next turn sees it
# ==================================================================================================
@pytest.mark.asyncio
async def test_moved_chat_gets_files_on_next_turn(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """A chat moved into a workspace via PATCH gets the workspace files from its NEXT message."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=10)
    w = await _create_workspace(client, uid, name="X", instructions=_INSTRUCTIONS)
    await _add_text_file(client, uid, str(w["id"]), content=_FILE_TEXT)

    # turn 0: a PLAIN chat (no workspace) — no instructions, no file.
    t0 = await _run(client, uid, fake_anthropic, message="first")
    sid = str(t0["sessionId"])
    assert _FILE_LABEL not in str(fake_anthropic.calls[0]["messages"])

    # PATCH the chat into the workspace.
    patch = await client.patch(
        f"/v1/chats/{sid}", json={"workspaceProjectId": str(w["id"])}, headers=auth_headers(uid)
    )
    assert patch.status_code == 200, patch.text

    # next turn: instructions AND the file are now injected (ADR-064 §6 closes Q-038-1, variant b).
    await _run(client, uid, fake_anthropic, message="what is the national animal?", session_id=sid)
    last = str(fake_anthropic.calls[-1]["messages"])
    assert last.count(_FILE_LABEL) == 1
    assert _FACT_B_TOKEN in last
    assert _INSTRUCTIONS in str(fake_anthropic.calls[-1]["system_prompt"])


# ==================================================================================================
# Case 4 — edited first message (Q-040-3): edit+regenerate re-injects the files
# ==================================================================================================
@pytest.mark.asyncio
async def test_edit_first_message_reinjects_files(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """Editing the first message (editMessageStepId, is_new=False after truncation) re-injects."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=10)
    w = await _create_workspace(client, uid, name="X", instructions=_INSTRUCTIONS)
    await _add_text_file(client, uid, str(w["id"]), content=_FILE_TEXT)

    t0 = await _run(client, uid, fake_anthropic, message="first", workspace_id=str(w["id"]))
    sid = str(t0["sessionId"])
    msid1 = str(t0["messageStepId"])
    assert _FILE_LABEL in str(fake_anthropic.calls[0]["messages"])

    # edit+regenerate the first message → whole history truncated, session resumed (is_new=False).
    edited = await _run(
        client,
        uid,
        fake_anthropic,
        message="first-edited",
        session_id=sid,
        edit_message_step_id=msid1,
    )
    assert str(edited["sessionId"]) == sid
    assert str(edited["messageStepId"]) != msid1

    last = str(fake_anthropic.calls[-1]["messages"])
    assert last.count(_FILE_LABEL) == 1
    assert _FACT_B_TOKEN in last


# ==================================================================================================
# Case 5 — NO persist / NO leak: after a multi-turn workspace session, neither history nor preview
#          nor the raw chat_steps carry the file text or base64 image blocks
# ==================================================================================================
@pytest.mark.asyncio
async def test_files_never_leak_into_user_facing_history(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """The live-injected knowledge (text `[Файл проекта:]` block + base64 image) never reaches the
    user-facing history (GET /v1/chats/{id}), the preview (GET /v1/chats), or the raw store."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=10)
    w = await _create_workspace(client, uid, name="X")
    await _add_text_file(client, uid, str(w["id"]), content=_FILE_TEXT)
    await _add_image_file(client, uid, str(w["id"]), data=_PNG)
    image_b64 = _b64(_PNG)

    # Two turns of the same session (both re-inject the files into create_message).
    t0 = await _run(client, uid, fake_anthropic, message="q1", workspace_id=str(w["id"]))
    sid = str(t0["sessionId"])
    await _run(client, uid, fake_anthropic, message="q2", session_id=sid)
    # Sanity: the model DID receive the file blocks on the live calls (so the leak check is real).
    assert _FILE_LABEL in str(fake_anthropic.calls[-1]["messages"])
    assert image_b64 in str(fake_anthropic.calls[-1]["attachments"].content_blocks)

    # (a) user-facing history carries neither the file text/label nor the base64 image.
    hist = await client.get(f"/v1/chats/{sid}", headers=auth_headers(uid))
    assert hist.status_code == 200, hist.text
    assert _FILE_LABEL not in hist.text
    assert _FACT_A_TOKEN not in hist.text
    assert _FACT_B_TOKEN not in hist.text
    assert image_b64 not in hist.text

    # (b) preview list carries neither.
    lst = await client.get("/v1/chats", headers=auth_headers(uid))
    assert lst.status_code == 200, lst.text
    assert _FILE_LABEL not in lst.text
    assert image_b64 not in lst.text

    # (c) raw store (chat_steps.payload) carries neither — files are truly not persisted.
    assert not await _store_contains(db_sessionmaker, sid, _FILE_LABEL)
    assert not await _store_contains(db_sessionmaker, sid, _FACT_A_TOKEN)
    assert not await _store_contains(db_sessionmaker, sid, image_b64)


# ==================================================================================================
# Case 6 — graceful on continuation: workspace deleted between turn 0 and turn 1 → turn 1 does NOT
#          500 (falls back to the base/plain path, no files)
# ==================================================================================================
@pytest.mark.asyncio
async def test_graceful_when_workspace_deleted_between_turns(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """Deleting the workspace between turns must not crash the next turn (graceful, no files)."""
    from app.chat.orchestrator import _system_prompt_for

    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=10)
    w = await _create_workspace(client, uid, name="X", instructions=_INSTRUCTIONS)
    await _add_text_file(client, uid, str(w["id"]), content=_FILE_TEXT)

    t0 = await _run(client, uid, fake_anthropic, message="first", workspace_id=str(w["id"]))
    sid = str(t0["sessionId"])
    assert _FILE_LABEL in str(fake_anthropic.calls[0]["messages"])

    # Delete the workspace (FK chat_sessions.workspace_project_id → SET NULL, migration 0011).
    d = await client.delete(f"/v1/workspaces/{w['id']}", headers=auth_headers(uid))
    assert d.status_code in (200, 204), d.text

    # next turn must succeed (NOT 500) and fall back to the base prompt with no files.
    t1 = await _run(client, uid, fake_anthropic, message="second", session_id=sid)
    assert t1["status"] == "assistant_message", t1
    last = fake_anthropic.calls[-1]
    assert _FILE_LABEL not in str(last["messages"])
    assert last["system_prompt"] == _system_prompt_for("chat")
