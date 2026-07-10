"""Integration tests for ADR-024 — domain normalization of GET /v1/chats/{id} payload +
``assistantMessage`` enrichment at status=tool_call (Q-024-1 variant A).

Normative coverage of:
- docs/modules/chats/09-testing.md §«Доменная нормализация payload (ADR-024)»;
- docs/modules/chat-orchestrator/09-testing.md §«Integration — История: доменная нормализация
  payload (ADR-024)» and §«assistantMessage при tool_call (ADR-024 п.3 / Q-024-1, вариант A)».

Real PostgreSQL container; Anthropic faked at the client boundary. CRITICAL fake invariant
(chat-orchestrator/09-testing): the *stored* assistant payload (chat_steps.payload) must carry the
raw provider id ``toolu_...`` and the UNDERSCORE wire tool name (``calendar_create_events``),
exactly as the real AnthropicClient persists it (anthropic_client.py: content_blocks keep the raw
name; tool_uses carry the domain dotted name). The shared FakeAnthropicClient.tool_result stores a
DOT name in content_blocks (already-normalized), which would not exercise normalization — so these
tests build the AnthropicResult locally to mirror production: content_blocks = underscore name +
toolu_ id; tool_uses = domain dot name + same toolu_ id.

Scenarios 1-6 cover history normalization; 7-10 cover assistantMessage at tool_call.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import FakeAnthropicClient, auth_headers, seed_user

# Domain (dot) ↔ wire (underscore) pair used throughout. calendar.create_events is the §39 example.
_DOMAIN_NAME = "calendar.create_events"
_WIRE_NAME = "calendar_create_events"


def _anthropic_result(
    *,
    wire_name: str,
    domain_name: str,
    provider_id: str,
    args: dict[str, Any],
    text: str = "",
) -> Any:
    """Build an AnthropicResult mirroring production parsing of a [optional text, tool_use] turn.

    content_blocks (→ stored verbatim in chat_steps.payload) carry the UNDERSCORE wire name + the
    raw provider ``toolu_...`` id (what the SDK returns). tool_uses (→ drive the orchestrator /
    tool_calls / toolCall.name) carry the DOMAIN dotted name + the same raw id. If ``text`` is set,
    a text block precedes the tool_use block in content_blocks and is reflected in ``.text``.
    """
    from app.chat.anthropic_client import AnthropicResult, AnthropicUsage

    usage = AnthropicUsage(
        input_tokens=10,
        output_tokens=5,
        model="claude-sonnet-4-5",
        cache_read_tokens=0,
        cache_write_tokens=0,
    )
    content_blocks: list[dict[str, Any]] = []
    if text:
        content_blocks.append({"type": "text", "text": text})
    content_blocks.append({"type": "tool_use", "id": provider_id, "name": wire_name, "input": args})
    return AnthropicResult(
        stop_reason="tool_use",
        content_blocks=content_blocks,
        usage=usage,
        text=text,
        tool_uses=[{"id": provider_id, "name": domain_name, "input": args}],
    )


def _text_result(fake: FakeAnthropicClient, text: str = "final answer") -> Any:
    return fake.text_result(text)


def _history_step_by_id(history: dict, step_id: str) -> dict | None:
    for step in history["steps"]:
        if step["id"] == step_id:
            return step
    return None


def _blocks(step: dict) -> list[dict[str, Any]]:
    return step.get("payload", {}).get("content", [])


def _tool_use_blocks(step: dict) -> list[dict[str, Any]]:
    return [b for b in _blocks(step) if isinstance(b, dict) and b.get("type") == "tool_use"]


def _tool_result_blocks(step: dict) -> list[dict[str, Any]]:
    return [b for b in _blocks(step) if isinstance(b, dict) and b.get("type") == "tool_result"]


# ============================================================================================
# Scenario 1+2+3: name is dot (== /v1/tools, == /chat/run toolCall.name); ids are domain
# (== toolCall.id == /chat/run); provider toolu_ / providerToolUseId absent; text + tool_use.input
# byte-for-byte; the [text, tool_use] step is returned in full (both blocks present).
# ============================================================================================
@pytest.mark.asyncio
async def test_history_normalizes_name_and_ids_to_domain_full_step(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)

    provider_id = "toolu_01ScenarioOne"
    args = {
        "events": [
            {
                "title": "Standup",
                "start": "2026-06-10T09:00:00Z",
                "end": "2026-06-10T09:15:00Z",
            }
        ]
    }
    fake_anthropic.responses = [
        _anthropic_result(
            wire_name=_WIRE_NAME,
            domain_name=_DOMAIN_NAME,
            provider_id=provider_id,
            args=args,
            text="Sure, scheduling that for you.",
        ),
    ]

    run = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "projectId": "p", "message": "schedule it", "mode": "credits"},
        headers=auth_headers(uid),
    )
    rb = run.json()
    assert rb["status"] == "tool_call", rb
    sid = rb["sessionId"]
    domain_tool_call_id = rb["toolCall"]["id"]
    step_id = rb["stepId"]

    # /chat/run toolCall.name is the dot domain name (sanity for "== toolCall.name of same call").
    assert rb["toolCall"]["name"] == _DOMAIN_NAME

    # /v1/tools advertises the same dot name.
    tools = (await client.get("/v1/tools", headers=auth_headers(uid))).json()["tools"]
    assert _DOMAIN_NAME in {t["name"] for t in tools}

    # History.
    hist = (await client.get(f"/v1/chats/{sid}", headers=auth_headers(uid))).json()
    carrier = _history_step_by_id(hist, step_id)
    assert carrier is not None
    blocks = _blocks(carrier)

    # Scenario 3: the [text, tool_use] step is returned in FULL — both blocks, original order.
    assert [b["type"] for b in blocks] == ["text", "tool_use"]
    text_block = blocks[0]
    tu = blocks[1]

    # Scenario 1: name == dot == /v1/tools == /chat/run toolCall.name of the same call.
    assert tu["name"] == _DOMAIN_NAME

    # Scenario 2: tool_use.id == domain tool_calls.id (== /chat/run toolCall.id), not toolu_.
    assert tu["id"] == domain_tool_call_id
    assert not tu["id"].startswith("toolu_")

    # Scenario 3 (continued): text block + tool_use.input byte-for-byte as stored.
    assert text_block["text"] == "Sure, scheduling that for you."
    assert tu["input"] == args

    # No provider id leaks anywhere in the response.
    assert "toolu_" not in run.text  # /chat/run already domain (sanity)
    blob = (await client.get(f"/v1/chats/{sid}", headers=auth_headers(uid))).text
    assert "toolu_" not in blob
    assert "providerToolUseId" not in blob


@pytest.mark.asyncio
async def test_history_client_tool_roundtrip_no_provider_id_domain_tool_call_id(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """Scenario 2 (real client-tool flow): run → tool-result → final. The orchestrator stores the
    client tool_result as a tool step in its custom shape ({toolCallId, providerToolUseId, ...}),
    NOT a wire ``content[].type=tool_result`` block. History MUST: (a) strip providerToolUseId →
    no toolu_ anywhere; (b) keep the tool step's domain ``toolCallId`` == the same domain
    tool_calls.id as the tool_use block (== /chat/run toolCall.id)."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)

    provider_id = "toolu_01RoundTrip"
    fake_anthropic.responses = [
        _anthropic_result(
            wire_name="files_read",
            domain_name="files.read",
            provider_id=provider_id,
            args={"path": "a.txt"},
        ),
        _text_result(fake_anthropic, "done"),
    ]

    run = (
        await client.post(
            "/v1/chat/run",
            json={"userId": str(uid), "projectId": "p", "message": "read", "mode": "credits"},
            headers=auth_headers(uid),
        )
    ).json()
    assert run["status"] == "tool_call", run
    sid = run["sessionId"]
    tcid = run["toolCall"]["id"]

    tr = (
        await client.post(
            "/v1/chat/tool-result",
            json={
                "userId": str(uid),
                "sessionId": sid,
                "toolCallId": tcid,
                "result": {"content": "hello"},
            },
            headers=auth_headers(uid),
        )
    ).json()
    assert tr["status"] == "assistant_message", tr

    hist = (await client.get(f"/v1/chats/{sid}", headers=auth_headers(uid))).json()
    blob = (await client.get(f"/v1/chats/{sid}", headers=auth_headers(uid))).text
    assert "toolu_" not in blob  # provider id never leaks (providerToolUseId stripped)
    assert "providerToolUseId" not in blob

    # The assistant tool_use block resolves to the domain tcid.
    tool_use_ids = [b["id"] for step in hist["steps"] for b in _tool_use_blocks(step)]
    assert tcid in tool_use_ids
    assert all(not i.startswith("toolu_") for i in tool_use_ids)

    # The client tool step carries the domain toolCallId (== tcid), not a provider id.
    tool_steps = [st for st in hist["steps"] if st["role"] == "tool"]
    assert tool_steps, "expected a client tool step in history"
    assert any(st["payload"].get("toolCallId") == tcid for st in tool_steps)


@pytest.mark.asyncio
async def test_history_tool_result_block_tool_use_id_normalized_to_domain(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Scenario 2 (tool_result block path, §41): a stored payload whose ``content`` carries a wire
    ``tool_result`` block with a provider ``tool_use_id`` is normalized in history to the SAME
    domain tool_calls.id as its paired tool_use (no toolu_ leaks). The orchestrator does not emit
    such a block today, so it is seeded directly to exercise ``_normalize_tool_result_block``."""
    import json

    from sqlalchemy import text as sql

    async with db_sessionmaker() as s:
        uid = await seed_user(s)
        sid = uuid.uuid4()
        msid = uuid.uuid4()
        domain_tc_id = uuid.uuid4()
        provider_id = "toolu_01PairedResult"
        await s.execute(
            sql(
                "INSERT INTO chat_sessions "
                "(id, user_id, project_id, mode, assistant_mode, is_pinned, created_at, updated_at)"
                " VALUES (:id, :uid, 'p', 'credits', 'chat', false, now(), now())"
            ),
            {"id": str(sid), "uid": str(uid)},
        )
        await s.execute(
            sql(
                "INSERT INTO tool_calls "
                "(id, session_id, message_step_id, tool_name, provider_tool_use_id, args, status) "
                "VALUES (:id, :sid, :msid, 'files.read', :ptu, CAST('{}' AS JSONB), 'completed')"
            ),
            {
                "id": str(domain_tc_id),
                "sid": str(sid),
                "msid": str(msid),
                "ptu": provider_id,
            },
        )
        # assistant tool_use block (provider id + underscore name).
        await s.execute(
            sql(
                "INSERT INTO chat_steps "
                "(id, session_id, message_step_id, role, payload, created_at) "
                "VALUES (:id, :sid, :msid, 'assistant', CAST(:p AS JSONB), now())"
            ),
            {
                "id": str(uuid.uuid4()),
                "sid": str(sid),
                "msid": str(msid),
                "p": json.dumps(
                    {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": provider_id,
                                "name": "files_read",
                                "input": {"path": "a.txt"},
                            }
                        ]
                    }
                ),
            },
        )
        # a user step carrying a wire tool_result block (the §41 shape).
        await s.execute(
            sql(
                "INSERT INTO chat_steps "
                "(id, session_id, message_step_id, role, payload, created_at) "
                "VALUES (:id, :sid, :msid, 'user', CAST(:p AS JSONB), now())"
            ),
            {
                "id": str(uuid.uuid4()),
                "sid": str(sid),
                "msid": str(msid),
                "p": json.dumps(
                    {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": provider_id,
                                "content": "ok",
                                "is_error": False,
                            }
                        ]
                    }
                ),
            },
        )
        await s.commit()

    hist = (await client.get(f"/v1/chats/{sid}", headers=auth_headers(uid))).json()
    blob = (await client.get(f"/v1/chats/{sid}", headers=auth_headers(uid))).text
    assert "toolu_" not in blob  # neither tool_use.id nor tool_result.tool_use_id leaks provider id

    tool_use_ids = [b["id"] for step in hist["steps"] for b in _tool_use_blocks(step)]
    tool_result_ids = [
        b["tool_use_id"] for step in hist["steps"] for b in _tool_result_blocks(step)
    ]
    assert tool_use_ids == [str(domain_tc_id)]
    # §41: tool_result.tool_use_id == the SAME domain id as the paired tool_use.
    assert tool_result_ids == [str(domain_tc_id)]


# ============================================================================================
# Scenario 3 (parallel tool_use): two tool_use blocks in one assistant turn → each block gets its
# own DISTINCT domain id; both differ from the provider toolu_ ids.
# ============================================================================================
@pytest.mark.asyncio
async def test_history_parallel_tool_use_each_own_domain_id(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)

    from app.chat.anthropic_client import AnthropicResult, AnthropicUsage

    pid_a = "toolu_01ParallelA"
    pid_b = "toolu_01ParallelB"
    usage = AnthropicUsage(
        input_tokens=10,
        output_tokens=5,
        model="claude-sonnet-4-5",
        cache_read_tokens=0,
        cache_write_tokens=0,
    )
    # One assistant turn with two parallel tool_use blocks (underscore names + toolu_ ids stored).
    parallel = AnthropicResult(
        stop_reason="tool_use",
        content_blocks=[
            {"type": "tool_use", "id": pid_a, "name": "files_read", "input": {"path": "a.txt"}},
            {
                "type": "tool_use",
                "id": pid_b,
                "name": "files_list",
                "input": {"path": ".", "recursive": False},
            },
        ],
        usage=usage,
        text="",
        tool_uses=[
            {"id": pid_a, "name": "files.read", "input": {"path": "a.txt"}},
            {"id": pid_b, "name": "files.list", "input": {"path": ".", "recursive": False}},
        ],
    )
    fake_anthropic.responses = [parallel]

    run = (
        await client.post(
            "/v1/chat/run",
            json={"userId": str(uid), "projectId": "p", "message": "go", "mode": "credits"},
            headers=auth_headers(uid),
        )
    ).json()
    assert run["status"] == "tool_call", run
    sid = run["sessionId"]

    hist = (await client.get(f"/v1/chats/{sid}", headers=auth_headers(uid))).json()
    blob = (await client.get(f"/v1/chats/{sid}", headers=auth_headers(uid))).text
    assert "toolu_" not in blob

    all_tu: list[dict[str, Any]] = []
    for step in hist["steps"]:
        all_tu.extend(_tool_use_blocks(step))
    assert len(all_tu) == 2, all_tu
    ids = [b["id"] for b in all_tu]
    # Each parallel tool_use has its OWN distinct domain id; neither is a provider id.
    assert ids[0] != ids[1]
    assert all(not i.startswith("toolu_") for i in ids)
    # Names normalized to dot.
    assert {b["name"] for b in all_tu} == {"files.read", "files.list"}


# ============================================================================================
# Scenario 4: storage is NOT mutated — after serving history, chat_steps.payload in the DB still
# holds the underscore wire name + provider toolu_ id (normalization is on a copy at serialization).
# ============================================================================================
@pytest.mark.asyncio
async def test_storage_not_mutated_after_history_served(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)

    provider_id = "toolu_01StorageImmutable"
    fake_anthropic.responses = [
        _anthropic_result(
            wire_name=_WIRE_NAME,
            domain_name=_DOMAIN_NAME,
            provider_id=provider_id,
            args={"events": []},
            text="ok",
        ),
    ]
    run = (
        await client.post(
            "/v1/chat/run",
            json={"userId": str(uid), "projectId": "p", "message": "go", "mode": "credits"},
            headers=auth_headers(uid),
        )
    ).json()
    sid = run["sessionId"]
    assert run["status"] == "tool_call", run

    # Serve history (would mutate state if normalization were in-place).
    await client.get(f"/v1/chats/{sid}", headers=auth_headers(uid))

    # Inspect the stored payload directly via the repository (ground truth, not the API view).
    from app.chats.repository import ChatsRepository

    async with db_sessionmaker() as s:
        repo = ChatsRepository(s)
        steps = await repo.list_steps(uuid.UUID(sid))
    tool_use_steps = [
        st
        for st in steps
        if any(
            isinstance(b, dict) and b.get("type") == "tool_use"
            for b in st.payload.get("content", [])
        )
    ]
    assert tool_use_steps, "expected a stored assistant tool_use step"
    stored = tool_use_steps[0].payload
    stored_tu = next(b for b in stored["content"] if b.get("type") == "tool_use")
    # Storage still wire-valid for Anthropic replay: underscore name + raw provider id.
    assert stored_tu["name"] == _WIRE_NAME
    assert stored_tu["id"] == provider_id


# ============================================================================================
# Scenario 5: the provider→domain map is built with a SINGLE query (no N+1) — even with a
# multi-round tool-loop. Spy on ChatsRepository.provider_id_to_domain_id: serving history calls it
# exactly once (the method itself issues one query, asserted structurally in repository.py).
# ============================================================================================
@pytest.mark.asyncio
async def test_history_builds_map_with_single_query_no_n_plus_1(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)

    # Multi-round tool-loop: 3 client-tool rounds then a final text answer → many tool_use /
    # tool_result blocks across many steps (would trigger N+1 if resolved per-block).
    fake_anthropic.responses = [
        _anthropic_result(
            wire_name="files_read",
            domain_name="files.read",
            provider_id="toolu_01R1",
            args={"path": "1.txt"},
        ),
        _anthropic_result(
            wire_name="files_read",
            domain_name="files.read",
            provider_id="toolu_01R2",
            args={"path": "2.txt"},
        ),
        _anthropic_result(
            wire_name="files_read",
            domain_name="files.read",
            provider_id="toolu_01R3",
            args={"path": "3.txt"},
        ),
        _text_result(fake_anthropic, "all read"),
    ]

    run = (
        await client.post(
            "/v1/chat/run",
            json={"userId": str(uid), "projectId": "p", "message": "go", "mode": "credits"},
            headers=auth_headers(uid),
        )
    ).json()
    sid = run["sessionId"]
    tcid = run["toolCall"]["id"]
    # Drive the loop through all rounds via tool-result calls.
    for _ in range(3):
        tr = (
            await client.post(
                "/v1/chat/tool-result",
                json={
                    "userId": str(uid),
                    "sessionId": sid,
                    "toolCallId": tcid,
                    "result": {"content": "x"},
                },
                headers=auth_headers(uid),
            )
        ).json()
        if tr["status"] == "assistant_message":
            break
        tcid = tr["toolCall"]["id"]

    # Spy: count invocations of the single-query map builder during one history fetch.
    from app.chats.repository import ChatsRepository

    calls = {"n": 0}
    original = ChatsRepository.provider_id_to_domain_id

    async def _spy(self: ChatsRepository, session_id: uuid.UUID) -> dict[str, uuid.UUID]:
        calls["n"] += 1
        return await original(self, session_id)

    monkeypatch.setattr(ChatsRepository, "provider_id_to_domain_id", _spy)

    hist = (await client.get(f"/v1/chats/{sid}", headers=auth_headers(uid))).json()
    # Sanity: many tool_use blocks across the multi-round loop (would be N≥3 lookups if per-block).
    total_tu = sum(len(_tool_use_blocks(st)) for st in hist["steps"])
    assert total_tu >= 3, total_tu
    # The map is built ONCE per history fetch (single query, no N+1).
    assert calls["n"] == 1


# ============================================================================================
# Scenario 6: defensive — a provider id with no tool_calls row / an unknown wire name in payload →
# the block is returned AS-IS, response is NOT 500.
# ============================================================================================
@pytest.mark.asyncio
async def test_history_defensive_unknown_name_and_orphan_id_no_500(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """No orchestrator path produces an unknown name / orphan id (BUG-4 says tool_calls cover every
    tool_use), so this anomaly is seeded directly into chat_steps to assert the defensive branch."""
    from sqlalchemy import text as sql

    async with db_sessionmaker() as s:
        uid = await seed_user(s)
        sid = uuid.uuid4()
        msid = uuid.uuid4()
        await s.execute(
            sql(
                "INSERT INTO chat_sessions "
                "(id, user_id, project_id, mode, assistant_mode, is_pinned, created_at, updated_at)"
                " VALUES (:id, :uid, 'p', 'credits', 'chat', false, now(), now())"
            ),
            {"id": str(sid), "uid": str(uid)},
        )
        # assistant step with an unknown wire name and a provider id absent from tool_calls.
        import json

        payload = {
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_ORPHAN_NO_ROW",
                    "name": "totally_unknown_tool",
                    "input": {"x": 1},
                }
            ]
        }
        await s.execute(
            sql(
                "INSERT INTO chat_steps "
                "(id, session_id, message_step_id, role, payload, created_at) "
                "VALUES (:id, :sid, :msid, 'assistant', CAST(:p AS JSONB), now())"
            ),
            {
                "id": str(uuid.uuid4()),
                "sid": str(sid),
                "msid": str(msid),
                "p": json.dumps(payload),
            },
        )
        await s.commit()

    r = await client.get(f"/v1/chats/{sid}", headers=auth_headers(uid))
    assert r.status_code == 200, r.text  # defensive: never 500
    hist = r.json()
    tu = _tool_use_blocks(hist["steps"][0])[0]
    # Unknown name left as-is; orphan provider id left as-is (no crash, no silent drop to null).
    assert tu["name"] == "totally_unknown_tool"
    assert tu["id"] == "toolu_ORPHAN_NO_ROW"


# ============================================================================================
# Scenario 7: [text, tool_use] → /chat/run AND /chat/tool-result status=tool_call with non-empty
# toolCall AND non-empty assistantMessage (== concatenation of the step's text blocks).
# ============================================================================================
@pytest.mark.asyncio
async def test_tool_call_carries_assistant_message_run_and_tool_result(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)

    # Round 1: [text, tool_use]; round 2 (after tool-result): again [text, tool_use]; round 3 final.
    fake_anthropic.responses = [
        _anthropic_result(
            wire_name="files_read",
            domain_name="files.read",
            provider_id="toolu_01T1",
            args={"path": "a.txt"},
            text="Let me check the first file.",
        ),
        _anthropic_result(
            wire_name="files_read",
            domain_name="files.read",
            provider_id="toolu_01T2",
            args={"path": "b.txt"},
            text="Now the second one.",
        ),
        _text_result(fake_anthropic, "done"),
    ]

    run = (
        await client.post(
            "/v1/chat/run",
            json={"userId": str(uid), "projectId": "p", "message": "go", "mode": "credits"},
            headers=auth_headers(uid),
        )
    ).json()
    assert run["status"] == "tool_call", run
    assert run["toolCall"] is not None
    assert run["assistantMessage"] == "Let me check the first file."
    sid = run["sessionId"]
    tcid = run["toolCall"]["id"]

    tr = (
        await client.post(
            "/v1/chat/tool-result",
            json={
                "userId": str(uid),
                "sessionId": sid,
                "toolCallId": tcid,
                "result": {"content": "x"},
            },
            headers=auth_headers(uid),
        )
    ).json()
    # /chat/tool-result also carries the accompanying text at status=tool_call.
    assert tr["status"] == "tool_call", tr
    assert tr["toolCall"] is not None
    assert tr["assistantMessage"] == "Now the second one."


# ============================================================================================
# Scenario 8: tool_use WITHOUT text → assistantMessage is null/omitted at status=tool_call.
# ============================================================================================
@pytest.mark.asyncio
async def test_tool_call_without_text_has_null_assistant_message(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [
        _anthropic_result(
            wire_name="files_read",
            domain_name="files.read",
            provider_id="toolu_01NoText",
            args={"path": "a.txt"},
            text="",  # no text block
        ),
    ]
    run = (
        await client.post(
            "/v1/chat/run",
            json={"userId": str(uid), "projectId": "p", "message": "go", "mode": "credits"},
            headers=auth_headers(uid),
        )
    ).json()
    assert run["status"] == "tool_call", run
    assert run["toolCall"] is not None
    assert run.get("assistantMessage") is None


# ============================================================================================
# Scenario 9: assistantMessage at tool_call is byte-for-byte == the text of the stepId step in
# GET /v1/chats/{id} (same string in the run projection and in history).
# ============================================================================================
@pytest.mark.asyncio
async def test_tool_call_assistant_message_matches_history_step_text(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)

    text_val = "I will read the configuration file now."
    fake_anthropic.responses = [
        _anthropic_result(
            wire_name="files_read",
            domain_name="files.read",
            provider_id="toolu_01Match",
            args={"path": "cfg.json"},
            text=text_val,
        ),
    ]
    run = (
        await client.post(
            "/v1/chat/run",
            json={"userId": str(uid), "projectId": "p", "message": "go", "mode": "credits"},
            headers=auth_headers(uid),
        )
    ).json()
    assert run["status"] == "tool_call", run
    sid = run["sessionId"]
    step_id = run["stepId"]
    assert run["assistantMessage"] == text_val

    hist = (await client.get(f"/v1/chats/{sid}", headers=auth_headers(uid))).json()
    carrier = _history_step_by_id(hist, step_id)
    assert carrier is not None
    # Concatenate the text blocks of the carrier step (normalization leaves text untouched).
    hist_text = "".join(
        b.get("text", "")
        for b in _blocks(carrier)
        if isinstance(b, dict) and b.get("type") == "text"
    )
    assert hist_text == text_val == run["assistantMessage"]


# ============================================================================================
# Scenario 10: regression — assistant_message (final) carries the final text unchanged; blocked
# carries assistantMessage == null. (No ADR-024 change to these two paths.)
# ============================================================================================
@pytest.mark.asyncio
async def test_regression_final_assistant_message_and_blocked(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    # Final assistant_message: text preserved as-is.
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [_text_result(fake_anthropic, "the final answer")]
    run = (
        await client.post(
            "/v1/chat/run",
            json={"userId": str(uid), "projectId": "p", "message": "hi", "mode": "credits"},
            headers=auth_headers(uid),
        )
    ).json()
    assert run["status"] == "assistant_message", run
    assert run["assistantMessage"] == "the final answer"
    assert run["toolCall"] is None

    # blocked: assistantMessage null (trial used, no subscription, mode=credits → trial_used).
    async with db_sessionmaker() as s:
        blocked_uid = await seed_user(s, trial_used=True)
    blocked = (
        await client.post(
            "/v1/chat/run",
            json={
                "userId": str(blocked_uid),
                "projectId": "p",
                "message": "hi",
                "mode": "credits",
            },
            headers=auth_headers(blocked_uid),
        )
    ).json()
    assert blocked["status"] == "blocked", blocked
    assert blocked.get("assistantMessage") is None
    assert blocked["blockReason"] == "trial_used"
