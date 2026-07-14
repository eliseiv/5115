"""Integration tests for ADR-036 §3: workspace instructions re-injected on continuation.

Major-fix regression coverage. The `instructions` of a workspace live in the `system` param (NOT
in the message history), so they must be re-injected into the system prompt on EVERY LLM call,
including the `/chat/tool-result` continuation that closes a client-side tool round. Before the fix
the continuation used the base assistant_mode prompt and dropped the workspace instructions.

Pattern: FakeAnthropicClient records `calls[*]["system_prompt"]`; turn 0 (`/chat/run`) scripts a
client-side tool_call, then `/chat/tool-result` drives the continuation. We assert the system_prompt
of the CONTINUATION call (calls[-1]) equals the turn-0 system_prompt (calls[0]) and carries the
instructions.

Real PostgreSQL container; Anthropic faked at the client boundary (06-testing-strategy.md).
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import FakeAnthropicClient, auth_headers, seed_user
from tests.fake_client_tool import FAKE_CLIENT_TOOL, register_fake_client_tool


@pytest.fixture(autouse=True)
def _register_fake_client_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    # ADR-063: register a test-only client-side example tool (no shipped client-side tool remains).
    register_fake_client_tool(monkeypatch)


async def _create_workspace(
    client: AsyncClient, uid: uuid.UUID, **body: object
) -> dict[str, object]:
    payload: dict[str, object] = {"name": "Proj"}
    payload.update(body)
    r = await client.post("/v1/workspaces", json=payload, headers=auth_headers(uid))
    assert r.status_code == 201, r.text
    return r.json()


async def _run_then_continue(
    client: AsyncClient,
    uid: uuid.UUID,
    fake: FakeAnthropicClient,
    *,
    workspace_id: str | None,
) -> tuple[dict[str, object], dict[str, object]]:
    """turn 0: /chat/run → client-side tool_call; then /chat/tool-result → assistant_message.

    Returns (run_body, continuation_body). After this, fake.calls has exactly two entries:
    calls[0] = turn-0 create_message, calls[-1] = continuation create_message.
    """
    fake.responses = [
        fake.tool_result(FAKE_CLIENT_TOOL, {"path": "a.txt"}),
        fake.text_result("the answer"),
    ]
    body: dict[str, object] = {"userId": str(uid), "message": "go", "mode": "credits"}
    if workspace_id is not None:
        body["workspaceProjectId"] = workspace_id
    r1 = await client.post("/v1/chat/run", json=body, headers=auth_headers(uid))
    assert r1.status_code == 200, r1.text
    b1 = r1.json()
    assert b1["status"] == "tool_call", b1
    sid = b1["sessionId"]
    tcid = b1["toolCall"]["id"]

    r2 = await client.post(
        "/v1/chat/tool-result",
        json={"userId": str(uid), "sessionId": sid, "toolCallId": tcid, "result": {"ok": 1}},
        headers=auth_headers(uid),
    )
    assert r2.status_code == 200, r2.text
    b2 = r2.json()
    assert b2["status"] == "assistant_message", b2
    return b1, b2


# ---------------------------------------------------------------------------
# KEY CASE: instructions re-injected on continuation (was lost before the fix)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_instructions_reinjected_on_tool_result_continuation(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """Continuation system_prompt == turn-0 system_prompt and contains the instructions."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    w = await _create_workspace(client, uid, name="X", instructions="ALWAYS_PIRATE_SPEAK")

    await _run_then_continue(client, uid, fake_anthropic, workspace_id=str(w["id"]))

    assert len(fake_anthropic.calls) == 2
    turn0_system = fake_anthropic.calls[0]["system_prompt"]
    continuation_system = fake_anthropic.calls[-1]["system_prompt"]
    # The instructions are present on BOTH the first turn and the continuation, identically.
    assert "ALWAYS_PIRATE_SPEAK" in turn0_system
    assert "ALWAYS_PIRATE_SPEAK" in continuation_system
    assert continuation_system == turn0_system

    from app.chat.orchestrator import _system_prompt_for

    # And it is genuinely base + instructions, not the bare base prompt (the pre-fix bug).
    assert continuation_system != _system_prompt_for("chat")


# ---------------------------------------------------------------------------
# Continuation: workspace WITHOUT instructions → base system prompt
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_continuation_no_instructions_uses_base_system(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """A workspace with empty/missing instructions → continuation uses the base prompt unchanged."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    w = await _create_workspace(client, uid, name="X")  # no instructions

    await _run_then_continue(client, uid, fake_anthropic, workspace_id=str(w["id"]))

    from app.chat.orchestrator import _system_prompt_for

    base = _system_prompt_for("chat")
    assert fake_anthropic.calls[0]["system_prompt"] == base
    assert fake_anthropic.calls[-1]["system_prompt"] == base


@pytest.mark.asyncio
async def test_continuation_blank_instructions_uses_base_system(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """Whitespace-only instructions are treated as empty on continuation (no injection)."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    w = await _create_workspace(client, uid, name="X", instructions="   ")

    await _run_then_continue(client, uid, fake_anthropic, workspace_id=str(w["id"]))

    from app.chat.orchestrator import _system_prompt_for

    assert fake_anthropic.calls[-1]["system_prompt"] == _system_prompt_for("chat")


# ---------------------------------------------------------------------------
# Continuation: session WITHOUT workspaceProjectId → base system (unchanged path)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_continuation_plain_chat_uses_base_system(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """A plain chat (no workspace) is unaffected: continuation uses the base prompt."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)

    await _run_then_continue(client, uid, fake_anthropic, workspace_id=None)

    from app.chat.orchestrator import _system_prompt_for

    base = _system_prompt_for("chat")
    assert fake_anthropic.calls[0]["system_prompt"] == base
    assert fake_anthropic.calls[-1]["system_prompt"] == base


# ---------------------------------------------------------------------------
# ADR-064: knowledge files ARE re-injected on the first continuation call
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_knowledge_files_reinjected_on_continuation_adr064(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """Workspace knowledge files ride the FIRST /chat/tool-result continuation call (ADR-064).

    ADR-064 supersedes the former ADR-036 §6 «files turn-0 only» behaviour: knowledge files are
    LIVE per-turn context, never persisted, re-assembled via `context_for_session` and injected into
    the first LLM call of EVERY generation request — including the continuation that closes a
    client-side tool round. Here there is exactly ONE continuation call (calls[-1]); it must carry
    the knowledge blob exactly once (the pre-ADR-064 bug dropped it — the model lost the knowledge
    base after turn 0). The dedicated ADR-064 suite additionally proves it is NOT repeated on the
    internal server-tool rounds of the same request.
    """
    import base64

    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    w = await _create_workspace(client, uid, name="X")
    blob = "PROJECT_KNOWLEDGE_BLOB_UNIQUE"
    r = await client.post(
        f"/v1/workspaces/{w['id']}/files",
        json={
            "type": "text",
            "mediaType": "text/plain",
            "filename": "notes.txt",
            "data": base64.b64encode(blob.encode()).decode("ascii"),
        },
        headers=auth_headers(uid),
    )
    assert r.status_code == 201, r.text

    await _run_then_continue(client, uid, fake_anthropic, workspace_id=str(w["id"]))

    # turn 0 carries the knowledge text exactly once (assembled attachment block on the user turn).
    turn0_blob = str(fake_anthropic.calls[0]["messages"])
    assert turn0_blob.count(blob) == 1

    # ADR-064: the continuation RE-INJECTS the knowledge files (live per-turn context) — the blob is
    # present on the continuation call exactly once (was 0 before the fix — the regression).
    continuation_blob = str(fake_anthropic.calls[-1]["messages"])
    assert continuation_blob.count(blob) == 1
