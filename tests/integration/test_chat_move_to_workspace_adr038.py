"""Integration tests for ADR-038 — moving a chat into / out of a workspace via PATCH /v1/chats/{id}.

Real PostgreSQL container (testcontainers); the LLM client is faked at the create_message boundary
(conftest `FakeAnthropicClient`, the faithful `LLMClient` double — records `system_prompt` and the
WIRE messages). Anthropic is the default provider in tests (`LLM_PROVIDER` unset → "anthropic");
the conftest `client` fixture pins `anthropic_client._anthropic_singleton` to the fake. The
provider-agnostic case (case 8b) additionally mirrors the conftest seam on the OpenAI path by
pinning `llm_client._openai_singleton` to the same double, so the suite is hermetic for BOTH
providers and passes with placeholder/empty API keys (no network).

ADR-038 contract under test:
- PATCH `workspaceProjectId`: absent → binding untouched; uuid → re-bind (ownership-validated, a
  foreign/missing target → 404 workspace_not_found); null → unbind. Partial update of title/isPinned
  MUST NOT clobber the workspace binding (and vice-versa). Idempotent.
- Owner isolation: a foreign/missing CHAT → 404 (checked BEFORE the workspace ownership check).
- Orchestrator: a session bound to a workspace re-injects `workspace.instructions` into `system` on
  EVERY turn — including a NON-turn-0 run on a chat MOVED into the workspace later. Knowledge FILES
  are ALSO re-injected on every turn (ADR-064 §6 supersedes ADR-038 §3.2 variant a, closes
  Q-038-1): a moved chat retroactively gets the files from its next message.
- A plain (non-workspace) chat: system prompt is the base prompt, no (double) injection.

Covers follow_up_for_qa cases 1–9.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import Iterator

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from tests.conftest import FakeAnthropicClient, auth_headers, seed_user

_INSTRUCTIONS = "ALWAYS_REPLY_IN_HAIKU"
_KNOWLEDGE_BLOB = "WORKSPACE_KNOWLEDGE_BLOB_UNIQUE"


# --------------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------------
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


async def _run(
    client: AsyncClient,
    uid: uuid.UUID,
    fake: FakeAnthropicClient,
    *,
    message: str = "go",
    session_id: str | None = None,
    workspace_id: str | None = None,
    text_reply: str = "ok",
) -> dict[str, object]:
    """One `/chat/run` turn returning an assistant_message; returns the response body."""
    fake.responses = [fake.text_result(text_reply)]
    body: dict[str, object] = {"userId": str(uid), "message": message, "mode": "credits"}
    if session_id is not None:
        body["sessionId"] = session_id
    if workspace_id is not None:
        body["workspaceProjectId"] = workspace_id
    r = await client.post("/v1/chat/run", json=body, headers=auth_headers(uid))
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "assistant_message", out
    return out


async def _patch(
    client: AsyncClient, uid: uuid.UUID, chat_id: str, **body: object
) -> tuple[int, dict[str, object]]:
    r = await client.patch(f"/v1/chats/{chat_id}", json=body, headers=auth_headers(uid))
    return r.status_code, r.json()


def _last_system(fake: FakeAnthropicClient) -> str:
    return str(fake.calls[-1]["system_prompt"])


async def _binding(maker: async_sessionmaker[AsyncSession], session_id: str) -> uuid.UUID | None:
    async with maker() as s:
        return await s.scalar(
            text("SELECT workspace_project_id FROM chat_sessions WHERE id=:sid"),
            {"sid": session_id},
        )


@pytest.fixture
def restore_provider() -> Iterator[None]:
    s = get_settings()
    orig = s.llm_provider
    yield
    s.llm_provider = orig


# --------------------------------------------------------------------------------------------------
# Case 1 — PATCH workspaceProjectId=<own uuid> → 200; next (non-turn-0) run injects instructions
# --------------------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_patch_bind_then_next_run_injects_instructions(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """Move a plain chat into a workspace; the next run (not turn-0) gets instructions in system."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    w = await _create_workspace(client, uid, name="X", instructions=_INSTRUCTIONS)

    # turn 0: a plain chat (no workspace) — base system prompt, no instructions.
    run0 = await _run(client, uid, fake_anthropic, message="first")
    sid = str(run0["sessionId"])
    from app.chat.orchestrator import _system_prompt_for

    assert fake_anthropic.calls[0]["system_prompt"] == _system_prompt_for("chat")
    assert _INSTRUCTIONS not in fake_anthropic.calls[0]["system_prompt"]

    # PATCH: bind into the workspace.
    code, body = await _patch(client, uid, sid, workspaceProjectId=str(w["id"]))
    assert code == 200, body
    assert body["workspaceProjectId"] == w["id"]

    # next run on the SAME session (NOT turn 0) → instructions injected into system.
    await _run(client, uid, fake_anthropic, message="second", session_id=sid)
    last_system = _last_system(fake_anthropic)
    assert _INSTRUCTIONS in last_system
    assert last_system == f"{_system_prompt_for('chat')}\n\n{_INSTRUCTIONS}"


# --------------------------------------------------------------------------------------------------
# Case 2 — PATCH with foreign / non-existent workspace → 404 workspace_not_found (isolation by sub)
# --------------------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_patch_foreign_workspace_404(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        owner = await seed_user(s, subscription="active", balance=5)
        other = await seed_user(s, subscription="active", balance=5)
    other_ws = await _create_workspace(client, other, name="Theirs")

    run0 = await _run(client, owner, fake_anthropic)
    sid = str(run0["sessionId"])

    # Foreign workspace (belongs to `other`) → 404 workspace_not_found (never reveal existence).
    code, body = await _patch(client, owner, sid, workspaceProjectId=str(other_ws["id"]))
    assert code == 404, body
    assert body["error"]["code"] == "workspace_not_found", body

    # Non-existent workspace uuid → same 404 workspace_not_found.
    code2, body2 = await _patch(client, owner, sid, workspaceProjectId=str(uuid.uuid4()))
    assert code2 == 404, body2
    assert body2["error"]["code"] == "workspace_not_found", body2

    # Binding untouched (still NULL) after the failed re-bind.
    assert await _binding(db_sessionmaker, sid) is None


@pytest.mark.asyncio
async def test_patch_foreign_workspace_isolation_does_not_leak_owns(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """`other` cannot bind their OWN chat to `owner`'s workspace (sub-scoped ownership)."""
    async with db_sessionmaker() as s:
        owner = await seed_user(s, subscription="active", balance=5)
        other = await seed_user(s, subscription="active", balance=5)
    owner_ws = await _create_workspace(client, owner, name="Owners", instructions=_INSTRUCTIONS)

    run0 = await _run(client, other, fake_anthropic)
    sid = str(run0["sessionId"])
    code, body = await _patch(client, other, sid, workspaceProjectId=str(owner_ws["id"]))
    assert code == 404, body
    assert body["error"]["code"] == "workspace_not_found", body


# --------------------------------------------------------------------------------------------------
# Case 3 — PATCH workspaceProjectId=null unbinds; instructions no longer injected
# --------------------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_patch_null_unbinds_and_stops_injection(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    w = await _create_workspace(client, uid, name="X", instructions=_INSTRUCTIONS)

    # turn 0 created IN the workspace → instructions injected on turn 0.
    run0 = await _run(client, uid, fake_anthropic, message="first", workspace_id=str(w["id"]))
    sid = str(run0["sessionId"])
    assert _INSTRUCTIONS in fake_anthropic.calls[0]["system_prompt"]

    # PATCH null → unbind.
    code, body = await _patch(client, uid, sid, workspaceProjectId=None)
    assert code == 200, body
    assert body["workspaceProjectId"] is None
    assert await _binding(db_sessionmaker, sid) is None

    # next run is a plain chat: base system prompt, instructions NOT injected.
    from app.chat.orchestrator import _system_prompt_for

    await _run(client, uid, fake_anthropic, message="second", session_id=sid)
    last_system = _last_system(fake_anthropic)
    assert _INSTRUCTIONS not in last_system
    assert last_system == _system_prompt_for("chat")


# --------------------------------------------------------------------------------------------------
# Case 4 — PATCH without workspaceProjectId (only title/isPinned) → binding NOT changed
# --------------------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_patch_title_pin_does_not_clobber_binding(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    w = await _create_workspace(client, uid, name="X", instructions=_INSTRUCTIONS)

    run0 = await _run(client, uid, fake_anthropic, workspace_id=str(w["id"]))
    sid = str(run0["sessionId"])
    assert await _binding(db_sessionmaker, sid) == uuid.UUID(str(w["id"]))

    # Partial update of title + isPinned WITHOUT workspaceProjectId → binding preserved.
    code, body = await _patch(client, uid, sid, title="renamed", isPinned=True)
    assert code == 200, body
    assert body["title"] == "renamed"
    assert body["isPinned"] is True
    # workspaceProjectId echoed unchanged in the response AND in the DB.
    assert body["workspaceProjectId"] == w["id"]
    assert await _binding(db_sessionmaker, sid) == uuid.UUID(str(w["id"]))


# --------------------------------------------------------------------------------------------------
# Case 5 — idempotency: repeated PATCH same uuid / repeated null → 200
# --------------------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_patch_idempotent_same_uuid_and_repeated_null(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    w = await _create_workspace(client, uid, name="X")

    run0 = await _run(client, uid, fake_anthropic)
    sid = str(run0["sessionId"])

    for _ in range(2):
        code, body = await _patch(client, uid, sid, workspaceProjectId=str(w["id"]))
        assert code == 200, body
        assert body["workspaceProjectId"] == w["id"]
    assert await _binding(db_sessionmaker, sid) == uuid.UUID(str(w["id"]))

    for _ in range(2):
        code, body = await _patch(client, uid, sid, workspaceProjectId=None)
        assert code == 200, body
        assert body["workspaceProjectId"] is None
    assert await _binding(db_sessionmaker, sid) is None


# --------------------------------------------------------------------------------------------------
# Case 6 — foreign / non-existent CHAT → 404 (BEFORE any workspace check)
# --------------------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_patch_foreign_chat_404_before_workspace_check(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        owner = await seed_user(s, subscription="active", balance=5)
        other = await seed_user(s, subscription="active", balance=5)
    # A chat owned by `other`; `owner`'s own valid workspace as the (irrelevant) target.
    other_run = await _run(client, other, fake_anthropic)
    other_sid = str(other_run["sessionId"])
    owner_ws = await _create_workspace(client, owner, name="Mine")

    # `owner` patches `other`'s chat → 404 (chat not found), NOT workspace_not_found: the chat check
    # runs first, so a valid target workspace must not change the verdict.
    code, body = await _patch(client, owner, other_sid, workspaceProjectId=str(owner_ws["id"]))
    assert code == 404, body
    assert body["error"]["code"] == "not_found", body

    # A purely non-existent chat uuid → 404 not_found too.
    code2, body2 = await _patch(client, owner, str(uuid.uuid4()), title="x")
    assert code2 == 404, body2
    assert body2["error"]["code"] == "not_found", body2


# --------------------------------------------------------------------------------------------------
# Case 7 — regression: chat created IN a workspace keeps instructions+files; a MOVED chat does NOT
#           get the files retroactively (variant a)
# --------------------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_native_workspace_chat_has_instructions_and_files(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    w = await _create_workspace(client, uid, name="X", instructions=_INSTRUCTIONS)
    await _add_knowledge_file(client, uid, str(w["id"]), _KNOWLEDGE_BLOB)

    # turn 0 inside the workspace: instructions in system AND knowledge file in the user turn.
    await _run(client, uid, fake_anthropic, message="hi", workspace_id=str(w["id"]))
    assert _INSTRUCTIONS in fake_anthropic.calls[0]["system_prompt"]
    turn0_messages = str(fake_anthropic.calls[0]["messages"])
    assert turn0_messages.count(_KNOWLEDGE_BLOB) == 1


@pytest.mark.asyncio
async def test_moved_chat_gets_instructions_and_files_retroactively_adr064(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """A moved chat gets BOTH instructions and files on the next turn (ADR-064 §6 closes Q-038-1).

    ADR-064 supersedes ADR-038 §3.2 variant a: since file injection is decoupled from ctx.is_new,
    a chat moved into a workspace via PATCH gets the knowledge files from its next message (var b).
    """
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    w = await _create_workspace(client, uid, name="X", instructions=_INSTRUCTIONS)
    await _add_knowledge_file(client, uid, str(w["id"]), _KNOWLEDGE_BLOB)

    # plain chat turn 0 (no workspace): no instructions, no files.
    run0 = await _run(client, uid, fake_anthropic, message="first")
    sid = str(run0["sessionId"])
    assert _KNOWLEDGE_BLOB not in str(fake_anthropic.calls[0]["messages"])

    code, _ = await _patch(client, uid, sid, workspaceProjectId=str(w["id"]))
    assert code == 200

    # next turn: instructions injected (system) AND knowledge files re-assembled (ADR-064, Q-038-1).
    await _run(client, uid, fake_anthropic, message="second", session_id=sid)
    assert _INSTRUCTIONS in _last_system(fake_anthropic)
    assert _KNOWLEDGE_BLOB in str(fake_anthropic.calls[-1]["messages"])


# --------------------------------------------------------------------------------------------------
# Case 8 — regression: plain (non-workspace) chat → base system prompt, no (double) injection
# --------------------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_plain_chat_system_prompt_is_base_no_injection(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    from app.chat.orchestrator import _system_prompt_for

    base = _system_prompt_for("chat")
    run0 = await _run(client, uid, fake_anthropic, message="first")
    sid = str(run0["sessionId"])
    assert fake_anthropic.calls[0]["system_prompt"] == base

    # second turn on the same plain chat: still base; no instructions, no double-injection.
    await _run(client, uid, fake_anthropic, message="second", session_id=sid)
    last_system = _last_system(fake_anthropic)
    assert last_system == base
    # No "double instruction" artifact: the base appears exactly once (not base + base).
    assert last_system.count(base) == 1


@pytest.mark.asyncio
async def test_move_to_workspace_provider_agnostic_openai(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    restore_provider: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same move-then-inject behavior with provider=openai.

    Hermetic: with provider=openai the `get_llm_client` factory would otherwise build a REAL
    `OpenAIClient` whose `create_message` hits the OpenAI network (401 under a placeholder key in
    CI). We mirror conftest's anthropic-singleton patch on the OpenAI seam: pin
    `llm_client._openai_singleton` to the same faithful `LLMClient` double (`fake_anthropic`) so the
    factory returns the fake on the openai path — no `OpenAIClient()` construction, no network. The
    instructions live in `system` (provider-agnostic), so the assertion holds for either provider.
    """
    from app.chat import llm_client as llm_client_mod
    from app.chat.orchestrator import _system_prompt_for

    get_settings().llm_provider = "openai"
    monkeypatch.setattr(llm_client_mod, "_openai_singleton", fake_anthropic, raising=False)

    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    w = await _create_workspace(client, uid, name="X", instructions=_INSTRUCTIONS)

    run0 = await _run(client, uid, fake_anthropic, message="first")
    sid = str(run0["sessionId"])
    assert _INSTRUCTIONS not in fake_anthropic.calls[0]["system_prompt"]

    code, _ = await _patch(client, uid, sid, workspaceProjectId=str(w["id"]))
    assert code == 200

    await _run(client, uid, fake_anthropic, message="second", session_id=sid)
    last_system = _last_system(fake_anthropic)
    assert _INSTRUCTIONS in last_system
    assert last_system == f"{_system_prompt_for('chat')}\n\n{_INSTRUCTIONS}"


# --------------------------------------------------------------------------------------------------
# Case 9 — validator: empty body {} → 422
# --------------------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_patch_empty_body_422(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    run0 = await _run(client, uid, fake_anthropic)
    sid = str(run0["sessionId"])

    r = await client.patch(f"/v1/chats/{sid}", json={}, headers=auth_headers(uid))
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_patch_all_null_body_422(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """Explicit nulls for title+isPinned with NO workspaceProjectId present → 422 (no change)."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    run0 = await _run(client, uid, fake_anthropic)
    sid = str(run0["sessionId"])

    # title=null (present) but isPinned=null and workspaceProjectId absent: title present counts
    # as a requested change, so this is actually a VALID body (sets title to null). Assert 200 to
    # pin the absent-vs-null semantics, distinct from the truly-empty {} above.
    r = await client.patch(f"/v1/chats/{sid}", json={"title": None}, headers=auth_headers(uid))
    assert r.status_code == 200, r.text

    # isPinned=null only (no title, no workspaceProjectId present) → nothing requested → 422.
    r2 = await client.patch(f"/v1/chats/{sid}", json={"isPinned": None}, headers=auth_headers(uid))
    assert r2.status_code == 422, r2.text
