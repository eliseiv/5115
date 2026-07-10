"""Integration tests for ADR-030 — toolCallId in serverTools[] of the /chat/run response.

Real PostgreSQL container; Anthropic faked at the client boundary via the shared
FakeAnthropicClient (BUG-4 invariant: provider ids are realistic ``toolu_...``). server-side
tools (``time.now`` global, ``site.*`` project-scoped) are executed by the backend inside the
tool-loop; ADR-028 surfaces them as a COMPACT ``serverTools[]`` array, and ADR-030 adds a
DOMAIN ``toolCallId`` (uuid4 = ``tool_calls.id``) to every element.

Scenarios map 1:1 to the task brief (1..6):
1. server-side round → every serverTools[] element has a non-empty, valid uuid toolCallId.
2. CORRELATION INVARIANT: serverTools[i].toolCallId == the toolCallId of the matching tool step
   in GET /v1/chats/{id} (steps[].payload.toolCallId).
3. DOMAIN not PROVIDER: toolCallId is a domain uuid4, NOT the provider toolu_...; same id domain
   as client-side toolCalls[].id (which is also a domain uuid4).
4. MULTIPLE server-side calls in one turn → each has a UNIQUE toolCallId; all present and all
   correlate with history.
5. MANDATORY: the field is always present in every serverTools[] element; with an empty
   serverTools[] (policy-blocked / replay) the array is [].
6. ADDITIVITY / REGRESSION: legacy fields (toolName/status/summary) and client toolCalls[] are
   intact; the existing ADR-028 serverTools[] tests stay green (separate module).
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import FakeAnthropicClient, auth_headers, seed_user

# A site.write_file payload (project-scoped server-side tool).
_SITE_WRITE = {
    "path": "index.html",
    "content": "<h1>landing</h1>",
    "contentType": "text/html",
    "encoding": "utf8",
}


def _assert_valid_uuid(value: object) -> uuid.UUID:
    """The element must carry a non-empty string that parses as a UUID."""
    assert isinstance(value, str), f"toolCallId must be a string, got {type(value)}: {value!r}"
    assert value, "toolCallId must be non-empty"
    return uuid.UUID(value)  # raises ValueError if not a valid uuid → test fails


async def _history_tool_steps(
    client: AsyncClient, uid: uuid.UUID, sid: str
) -> list[dict[str, object]]:
    hist = (await client.get(f"/v1/chats/{sid}", headers=auth_headers(uid))).json()
    return [st for st in hist["steps"] if st["role"] == "tool"]


# ============================================================================================
# Scenario 1 — a server-side round → serverTools[] element has a non-empty, valid uuid toolCallId.
# ============================================================================================
@pytest.mark.asyncio
async def test_time_now_round_element_has_valid_uuid_tool_call_id(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [
        fake_anthropic.tool_result("time.now", {}, tool_id="toolu_tn01"),
        fake_anthropic.text_result("Today is Wednesday."),
    ]
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "what day is it?", "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "assistant_message", body
    assert len(body["serverTools"]) == 1
    entry = body["serverTools"][0]
    # ADR-030: toolCallId present, non-empty, valid uuid.
    _assert_valid_uuid(entry.get("toolCallId"))
    # Legacy ADR-028 fields intact alongside the new field.
    assert entry["toolName"] == "time.now"
    assert entry["status"] == "completed"
    assert entry["summary"] == "ok"


@pytest.mark.asyncio
async def test_site_write_element_has_valid_uuid_tool_call_id(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [
        fake_anthropic.tool_result("site.write_file", _SITE_WRITE, tool_id="toolu_sw01"),
        fake_anthropic.text_result("written"),
    ]
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "projectId": "p30", "message": "build", "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "assistant_message", body
    assert len(body["serverTools"]) == 1
    entry = body["serverTools"][0]
    _assert_valid_uuid(entry.get("toolCallId"))
    assert entry["toolName"] == "site.write_file"


# ============================================================================================
# Scenario 2 — CORRELATION INVARIANT: serverTools[i].toolCallId == steps[].payload.toolCallId of
# the matching tool step in GET /v1/chats/{id}.
# ============================================================================================
@pytest.mark.asyncio
async def test_server_tool_call_id_correlates_with_history_tool_step(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [
        fake_anthropic.tool_result("time.now", {}, tool_id="toolu_corr1"),
        fake_anthropic.text_result("done"),
    ]
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "when?", "mode": "credits"},
        headers=auth_headers(uid),
    )
    body = r.json()
    assert body["status"] == "assistant_message", body
    sid = body["sessionId"]
    server_entry = body["serverTools"][0]
    response_tcid = server_entry["toolCallId"]
    _assert_valid_uuid(response_tcid)

    # The history has exactly one tool step whose payload.toolCallId == the response toolCallId,
    # and its toolName matches too (ADR-030 normative correlation invariant).
    tool_steps = await _history_tool_steps(client, uid, sid)
    matching = [
        st
        for st in tool_steps
        if st["payload"].get("toolCallId") == response_tcid
        and st["payload"].get("toolName") == server_entry["toolName"]
    ]
    assert len(matching) == 1, (
        f"expected exactly one history tool step with toolCallId={response_tcid!r} "
        f"and toolName={server_entry['toolName']!r}; got {len(matching)} "
        f"(all tool steps: {[s['payload'].get('toolCallId') for s in tool_steps]})"
    )


# ============================================================================================
# Scenario 3 — DOMAIN not PROVIDER: toolCallId is a domain uuid4, NOT the provider toolu_...;
# same id domain as client-side toolCalls[].id.
# ============================================================================================
@pytest.mark.asyncio
async def test_server_tool_call_id_is_domain_uuid_not_provider_id(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    # The provider id for this round is the realistic toolu_-shaped one below.
    fake_anthropic.responses = [
        fake_anthropic.tool_result("time.now", {}, tool_id="toolu_PROVIDER_dom3"),
        fake_anthropic.text_result("done"),
    ]
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "now", "mode": "credits"},
        headers=auth_headers(uid),
    )
    body = r.json()
    assert body["status"] == "assistant_message", body
    tcid = body["serverTools"][0]["toolCallId"]
    # It is a valid domain uuid4 (parses as uuid) and is NOT the provider toolu_... id.
    parsed = _assert_valid_uuid(tcid)
    assert parsed.version == 4, f"expected uuid4 domain id, got version {parsed.version}: {tcid}"
    assert not tcid.startswith("toolu_"), f"provider id leaked into toolCallId: {tcid}"
    assert "toolu_" not in tcid


@pytest.mark.asyncio
async def test_server_tool_call_id_same_domain_as_client_tool_calls_id(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """Symmetry: a turn with a server-side round THEN a client tool_call → serverTools[].toolCallId
    and toolCalls[].id are BOTH domain uuid4 (same id domain), and neither is the provider id."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [
        fake_anthropic.tool_result("time.now", {}, tool_id="toolu_sym_srv"),
        fake_anthropic.tool_result("files.read", {"path": "a.txt"}, tool_id="toolu_sym_cli"),
    ]
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "read with date", "mode": "credits"},
        headers=auth_headers(uid),
    )
    body = r.json()
    assert body["status"] == "tool_call", body
    server_tcid = body["serverTools"][0]["toolCallId"]
    client_tcid = body["toolCalls"][0]["id"]
    # Both are valid domain uuid4 — the SAME id domain (ADR-030 symmetry with ADR-008/ADR-024).
    assert _assert_valid_uuid(server_tcid).version == 4
    assert _assert_valid_uuid(client_tcid).version == 4
    # Neither leaks the provider id, and the two ids are distinct calls.
    assert not server_tcid.startswith("toolu_")
    assert not client_tcid.startswith("toolu_")
    assert server_tcid != client_tcid


# ============================================================================================
# Scenario 4 — MULTIPLE server-side calls in one turn → each has a UNIQUE toolCallId; all present
# and all correlate with history.
# ============================================================================================
@pytest.mark.asyncio
async def test_multi_round_server_side_unique_tool_call_ids_all_correlate(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    # Three server-side rounds, including a REPEATED tool (two time.now) — the case ADR-030 exists
    # for: toolName alone is ambiguous, toolCallId disambiguates.
    fake_anthropic.responses = [
        fake_anthropic.tool_result("site.write_file", _SITE_WRITE, tool_id="toolu_m1"),
        fake_anthropic.tool_result("time.now", {}, tool_id="toolu_m2"),
        fake_anthropic.tool_result("time.now", {"tz": "UTC"}, tool_id="toolu_m3"),
        fake_anthropic.text_result("all done"),
    ]
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "projectId": "multi30", "message": "go", "mode": "credits"},
        headers=auth_headers(uid),
    )
    body = r.json()
    assert body["status"] == "assistant_message", body
    server_tools = body["serverTools"]
    assert [e["toolName"] for e in server_tools] == ["site.write_file", "time.now", "time.now"]

    # Each element has a valid uuid toolCallId, and all are unique (incl. the two time.now rounds).
    tcids = [e["toolCallId"] for e in server_tools]
    for tcid in tcids:
        _assert_valid_uuid(tcid)
    assert len(set(tcids)) == len(tcids), f"toolCallIds must be unique per execution: {tcids}"

    # Every serverTools[] element correlates with exactly one history tool step (toolCallId+name).
    sid = body["sessionId"]
    tool_steps = await _history_tool_steps(client, uid, sid)
    hist_pairs = [
        (st["payload"].get("toolCallId"), st["payload"].get("toolName")) for st in tool_steps
    ]
    for entry in server_tools:
        pair = (entry["toolCallId"], entry["toolName"])
        assert hist_pairs.count(pair) == 1, (
            f"serverTools entry {pair} must match exactly one history tool step; "
            f"history pairs={hist_pairs}"
        )


# ============================================================================================
# Scenario 5 — MANDATORY: the field is always present; empty serverTools[] (policy-blocked /
# idempotent replay) is [] (no elements, so no missing-field concern).
# ============================================================================================
@pytest.mark.asyncio
async def test_policy_blocked_server_tools_empty_no_elements(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=0)
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "go", "mode": "credits"},
        headers=auth_headers(uid),
    )
    body = r.json()
    assert body["status"] == "blocked"
    assert body["blockReason"] == "credits_empty"
    # Empty list (no elements → no toolCallId to carry); never null/absent.
    assert body["serverTools"] == []
    assert fake_anthropic.calls == []


@pytest.mark.asyncio
async def test_idempotent_replay_continuation_server_tools_empty(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """A replayed /chat/tool-result of an already-closed turn → serverTools == [] (ADR-028 replay),
    which ADR-030 does not change (no elements, hence no toolCallId)."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [
        fake_anthropic.tool_result("files.read", {"path": "a.txt"}, tool_id="toolu_rep0"),
        fake_anthropic.text_result("final"),
    ]
    run = (
        await client.post(
            "/v1/chat/run",
            json={"userId": str(uid), "message": "go", "mode": "credits"},
            headers=auth_headers(uid),
        )
    ).json()
    assert run["status"] == "tool_call", run
    sid = run["sessionId"]
    tcid = run["toolCalls"][0]["id"]

    first = (
        await client.post(
            "/v1/chat/tool-result",
            json={"userId": str(uid), "sessionId": sid, "toolCallId": tcid, "result": {"ok": 1}},
            headers=auth_headers(uid),
        )
    ).json()
    assert first["status"] == "assistant_message", first
    # Replay the same already-completed tool-result → idempotent, serverTools == [].
    replay = (
        await client.post(
            "/v1/chat/tool-result",
            json={"userId": str(uid), "sessionId": sid, "toolCallId": tcid, "result": {"ok": 1}},
            headers=auth_headers(uid),
        )
    ).json()
    assert replay["serverTools"] == []


# ============================================================================================
# Scenario 6 — ADDITIVITY / REGRESSION: legacy serverTools fields + client toolCalls[] intact when
# toolCallId is added.
# ============================================================================================
@pytest.mark.asyncio
async def test_additivity_server_tools_element_keys_superset(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [
        fake_anthropic.tool_result("time.now", {}, tool_id="toolu_add1"),
        fake_anthropic.text_result("ok"),
    ]
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "now", "mode": "credits"},
        headers=auth_headers(uid),
    )
    body = r.json()
    entry = body["serverTools"][0]
    # Element carries the new field PLUS all legacy ADR-028 fields (additive change).
    assert set(entry.keys()) >= {"toolCallId", "toolName", "status", "summary"}
    assert entry["toolName"] == "time.now"
    assert entry["status"] == "completed"
    assert entry["summary"] == "ok"


@pytest.mark.asyncio
async def test_additivity_client_tool_calls_not_broken_by_server_tool_call_id(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """A mixed turn (server-side then client-side) → toolCalls[] unchanged shape (id/name/args),
    server-side stays only in serverTools[] (each with its own toolCallId)."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [
        fake_anthropic.tool_result("time.now", {}, tool_id="toolu_mx1"),
        fake_anthropic.tool_result("files.read", {"path": "a.txt"}, tool_id="toolu_mx2"),
    ]
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "read with date", "mode": "credits"},
        headers=auth_headers(uid),
    )
    body = r.json()
    assert body["status"] == "tool_call", body
    # client tool_call shape intact: id/name/args.
    tc = body["toolCalls"][0]
    assert set(tc.keys()) >= {"id", "name", "args"}
    assert tc["name"] == "files.read"
    _assert_valid_uuid(tc["id"])
    # server-side stays in serverTools[] only, and is NOT in toolCalls[].
    assert [e["toolName"] for e in body["serverTools"]] == ["time.now"]
    assert "time.now" not in {c["name"] for c in body["toolCalls"]}
    # The two ids are distinct domain calls.
    assert body["serverTools"][0]["toolCallId"] != tc["id"]
