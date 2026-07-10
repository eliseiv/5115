"""Integration tests for ADR-028 Решение 2 — serverTools[] in the /chat/run response.

Real PostgreSQL container; Anthropic faked at the client boundary via the shared
FakeAnthropicClient (BUG-4 invariant: provider ids are realistic ``toolu_...``). server-side
tools (``time.now`` global, ``site.*`` project-scoped) are executed by the backend inside the
tool-loop; ADR-028 surfaces them as a COMPACT ``serverTools[]`` array on the ChatResponse.

Scenarios map 1:1 to the task brief (2..9):
2. pure-text answer → serverTools == []
3. time.now round → {toolName:"time.now", status:"completed", summary:"ok"} (compact)
4. site.* (project session) → listed by domain toolName; SECURITY: summary leaks NO
   path/URL/signed-token from the raw site.* result (summary == "ok"/error_code only)
5. errored server-tool (time.now invalid tz) → status:"errored", summary == short error_code,
   the turn does NOT fail
6. status=tool_call (model asked client-side after a server-side round) → serverTools lists the
   server-side executed before the hand-off; client-side stays in toolCalls[]
7. blocked: policy-blocked → serverTools == []; max_tokens after a server-side round →
   serverTools may be NON-empty
8. multi-round server-side turn → serverTools accumulates all executions in order
9. additivity: legacy ChatResponse/ChatListItemSchema fields intact; serverTools defaults to []
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from tests.conftest import FakeAnthropicClient, auth_headers, seed_user

# A site.write_file whose RAW result would carry a path/URL/preview signed-token. The security
# assertion below proves none of that reaches serverTools[].summary.
_SITE_WRITE = {
    "path": "secret/index.html",
    "content": "<h1>top secret landing</h1>",
    "contentType": "text/html",
    "encoding": "utf8",
}


async def _count(maker: async_sessionmaker[AsyncSession], sql: str, uid: uuid.UUID) -> int:
    async with maker() as s:
        return int(await s.scalar(text(sql), {"u": str(uid)}) or 0)


def _assert_server_tool(
    entry: dict[str, object], *, toolName: str, status: str, summary: object
) -> None:
    """Assert the legacy ADR-028 fields of a serverTools[] element while tolerating the additive
    ADR-030 ``toolCallId`` (asserted to be a non-empty valid uuid). Using == on the whole dict broke
    once ADR-030 added ``toolCallId``; this keeps the original intent and stays forward-compatible.
    """
    assert entry["toolName"] == toolName, entry
    assert entry["status"] == status, entry
    assert entry["summary"] == summary, entry
    # ADR-030: every element carries a non-empty domain uuid toolCallId.
    tcid = entry.get("toolCallId")
    assert isinstance(tcid, str) and tcid, f"toolCallId must be a non-empty string: {entry}"
    uuid.UUID(tcid)  # raises if not a valid uuid → test fails


@pytest.fixture
def preview_secret() -> object:
    # site.preview builds an HMAC-signed URL; a configured secret makes the raw result carry a
    # real signed-token, sharpening the "summary does not leak the token" assertion.
    settings = get_settings()
    orig = settings.preview_url_secret
    settings.preview_url_secret = "adr028-secret-0123456789abcdef0123456789abcdef01"
    yield
    settings.preview_url_secret = orig


# ============================================================================================
# Scenario 2 — pure text answer (no server-side tools) → serverTools == [].
# ============================================================================================
@pytest.mark.asyncio
async def test_pure_text_answer_has_empty_server_tools(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [fake_anthropic.text_result("just a plain answer")]

    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "hello", "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "assistant_message"
    # Present and empty — never absent (the client must not distinguish missing vs []).
    assert body["serverTools"] == []


# ============================================================================================
# Scenario 3 — a time.now round → serverTools has one compact completed entry.
# ============================================================================================
@pytest.mark.asyncio
async def test_time_now_round_listed_completed_with_compact_summary(
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
    body = r.json()
    assert body["status"] == "assistant_message", body
    assert len(body["serverTools"]) == 1
    _assert_server_tool(
        body["serverTools"][0], toolName="time.now", status="completed", summary="ok"
    )
    # The compact summary must be tiny (≤ 120) and not the raw UTC/unix/weekday result dict.
    summary = body["serverTools"][0]["summary"]
    assert summary == "ok"
    assert len(summary) <= 120
    # time.now stays server-side — not surfaced as a client tool_call.
    assert body.get("toolCalls") in (None, [])


# ============================================================================================
# Scenario 4 — site.* (project session): listed by domain toolName; SECURITY — the summary
# leaks NO path/URL/preview signed-token from the raw site.* result.
# ============================================================================================
@pytest.mark.asyncio
async def test_site_write_file_listed_and_summary_does_not_leak_raw_result(
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
        json={"userId": str(uid), "projectId": "leak-proj", "message": "build", "mode": "credits"},
        headers=auth_headers(uid),
    )
    body = r.json()
    assert body["status"] == "assistant_message", body
    # Listed by the public dotted domain name.
    assert len(body["serverTools"]) == 1
    entry = body["serverTools"][0]
    assert entry["toolName"] == "site.write_file"
    assert entry["status"] == "completed"

    # SECURITY (ADR-028 §Решение2): the compact summary is "ok" — NOT the raw result. The
    # seeded path, any URL scheme and any signed-token marker must be absent from the summary.
    summary = entry["summary"]
    assert summary == "ok", summary
    assert len(summary) <= 120
    leak_markers = ["secret/index.html", "http", "://", "token=", "sig=", "expires"]
    lowered = (summary or "").lower()
    for marker in leak_markers:
        assert marker.lower() not in lowered, f"summary leaked {marker!r}: {summary!r}"
    # The file WAS written server-side (the round actually executed).
    async with db_sessionmaker() as s:
        files = int(await s.scalar(text("SELECT count(*) FROM site_files")) or 0)
    assert files == 1


@pytest.mark.asyncio
async def test_site_preview_summary_does_not_leak_signed_url(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    preview_secret: object,
) -> None:
    # site.preview's raw result carries an HMAC signed preview URL. ADR-028 forbids it leaking
    # into serverTools[].summary (compact indicator only).
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [
        # Write a file first so the project exists, then ask for a preview, then finalize.
        fake_anthropic.tool_result("site.write_file", _SITE_WRITE, tool_id="toolu_pw01"),
        fake_anthropic.tool_result("site.preview", {}, tool_id="toolu_pv01"),
        fake_anthropic.text_result("preview ready"),
    ]
    r = await client.post(
        "/v1/chat/run",
        json={
            "userId": str(uid),
            "projectId": "prev-proj",
            "message": "preview",
            "mode": "credits",
        },
        headers=auth_headers(uid),
    )
    body = r.json()
    assert body["status"] == "assistant_message", body
    names = [e["toolName"] for e in body["serverTools"]]
    assert names == ["site.write_file", "site.preview"]
    for entry in body["serverTools"]:
        summary = entry["summary"] or ""
        assert len(summary) <= 120
        # No URL / signed-token fragments leak through the summary.
        for marker in ["http", "://", "token=", "sig=", "expires", "/preview/"]:
            assert marker.lower() not in summary.lower(), (entry, summary)


# ============================================================================================
# Scenario 5 — errored server-tool (time.now with an invalid tz → invalid_timezone):
# status:"errored", summary == the short error_code, the turn DOES NOT fail.
# ============================================================================================
@pytest.mark.asyncio
async def test_errored_time_now_invalid_tz_listed_errored_short_code(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [
        # An invalid IANA tz passes args validation but the handler returns invalid_timezone.
        fake_anthropic.tool_result("time.now", {"tz": "Mars/Phobos"}, tool_id="toolu_err01"),
        fake_anthropic.text_result("could not resolve the timezone, here is UTC"),
    ]
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "time in Mars/Phobos?", "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert r.status_code == 200, r.text  # the turn survives — NOT a 5xx
    body = r.json()
    assert body["status"] == "assistant_message", body
    assert len(body["serverTools"]) == 1
    entry = body["serverTools"][0]
    assert entry["toolName"] == "time.now"
    assert entry["status"] == "errored"
    # summary == the short machine error code (not a stacktrace / detailed message).
    assert entry["summary"] == "invalid_timezone"
    assert len(entry["summary"]) <= 120


# ============================================================================================
# Scenario 6 — status=tool_call: model asks for a client-side tool AFTER a server-side round.
# serverTools lists the server-side executed before the hand-off; client-side stays in toolCalls[].
# ============================================================================================
@pytest.mark.asyncio
async def test_server_tools_present_when_turn_ends_in_client_tool_call(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    # Round 1: time.now (server-side) → loop continues. Round 2: files.read (client-side) →
    # hand-off to iOS with status=tool_call.
    fake_anthropic.responses = [
        fake_anthropic.tool_result("time.now", {}, tool_id="toolu_h1"),
        fake_anthropic.tool_result("files.read", {"path": "a.txt"}, tool_id="toolu_h2"),
    ]
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "read file with date", "mode": "credits"},
        headers=auth_headers(uid),
    )
    body = r.json()
    assert body["status"] == "tool_call", body
    # server-side executed before the hand-off is surfaced.
    assert len(body["serverTools"]) == 1
    _assert_server_tool(
        body["serverTools"][0], toolName="time.now", status="completed", summary="ok"
    )
    # client-side stays in toolCalls[] (NOT in serverTools).
    assert [tc["name"] for tc in body["toolCalls"]] == ["files.read"]
    server_names = {e["toolName"] for e in body["serverTools"]}
    assert "files.read" not in server_names


# ============================================================================================
# Scenario 7a — policy-blocked → serverTools == [] (tool-loop never ran).
# ============================================================================================
@pytest.mark.asyncio
async def test_policy_blocked_has_empty_server_tools(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    # Active subscription + balance 0 + mode=credits → credits_empty BEFORE generation.
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=0)
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "go", "mode": "credits"},
        headers=auth_headers(uid),
    )
    body = r.json()
    assert r.status_code == 200
    assert body["status"] == "blocked"
    assert body["blockReason"] == "credits_empty"
    # policy-block is before the tool-loop → empty, and Anthropic was never called.
    assert body["serverTools"] == []
    assert fake_anthropic.calls == []


# ============================================================================================
# Scenario 7b — max_tokens AFTER a server-side round → serverTools may be NON-empty.
# ============================================================================================
@pytest.mark.asyncio
async def test_max_tokens_after_server_round_has_non_empty_server_tools(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    # Round 1: time.now executes server-side. Round 2: the final turn is truncated by max_tokens.
    fake_anthropic.responses = [
        fake_anthropic.tool_result("time.now", {}, tool_id="toolu_mt1"),
        fake_anthropic.max_tokens_result(text="partial answer...", output_tokens=16000),
    ]
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "long task", "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "blocked"
    assert body["blockReason"] == "max_tokens"
    # Unlike policy-block: the server-side round that ran before truncation IS surfaced.
    assert len(body["serverTools"]) == 1
    _assert_server_tool(
        body["serverTools"][0], toolName="time.now", status="completed", summary="ok"
    )
    # Credit NOT debited (truncated generation is free, ADR-025).
    assert (
        await _count(
            db_sessionmaker,
            "SELECT count(*) FROM ledger_transactions WHERE user_id=:u AND type='debit'",
            uid,
        )
        == 0
    )


# ============================================================================================
# Scenario 8 — multi-round server-side turn → serverTools accumulates ALL executions in order.
# ============================================================================================
@pytest.mark.asyncio
async def test_multi_round_server_side_accumulates_in_order(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    # Three distinct server-side rounds (a site.write_file, a time.now, a site.list), then final.
    fake_anthropic.responses = [
        fake_anthropic.tool_result("site.write_file", _SITE_WRITE, tool_id="toolu_m1"),
        fake_anthropic.tool_result("time.now", {"tz": "UTC"}, tool_id="toolu_m2"),
        fake_anthropic.tool_result("site.list", {}, tool_id="toolu_m3"),
        fake_anthropic.text_result("all done"),
    ]
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "projectId": "multi-proj", "message": "go", "mode": "credits"},
        headers=auth_headers(uid),
    )
    body = r.json()
    assert body["status"] == "assistant_message", body
    # All three server-side executions, in execution order; each compact + completed.
    assert [e["toolName"] for e in body["serverTools"]] == [
        "site.write_file",
        "time.now",
        "site.list",
    ]
    assert all(e["status"] == "completed" for e in body["serverTools"])
    assert all(e["summary"] == "ok" for e in body["serverTools"])


# ============================================================================================
# Scenario 8b — tool-result continuation also carries serverTools for that call. A server-side
# round executed during a /chat/tool-result continuation is surfaced on THAT response.
# ============================================================================================
@pytest.mark.asyncio
async def test_tool_result_continuation_surfaces_its_server_tools(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    # run → client tool_call (files.read). After the client result, the continuation runs a
    # time.now server-side round, then finalizes.
    fake_anthropic.responses = [
        fake_anthropic.tool_result("files.read", {"path": "a.txt"}, tool_id="toolu_c0"),
        fake_anthropic.tool_result("time.now", {}, tool_id="toolu_c1"),
        fake_anthropic.text_result("final after continuation"),
    ]
    run = (
        await client.post(
            "/v1/chat/run",
            json={"userId": str(uid), "message": "go", "mode": "credits"},
            headers=auth_headers(uid),
        )
    ).json()
    assert run["status"] == "tool_call", run
    # The run turn had NO server-side execution before the client hand-off.
    assert run["serverTools"] == []
    sid = run["sessionId"]
    tcid = run["toolCalls"][0]["id"]

    tr = (
        await client.post(
            "/v1/chat/tool-result",
            json={"userId": str(uid), "sessionId": sid, "toolCallId": tcid, "result": {"ok": 1}},
            headers=auth_headers(uid),
        )
    ).json()
    assert tr["status"] == "assistant_message", tr
    # The continuation's own server-side round (time.now) is surfaced on the continuation response.
    assert len(tr["serverTools"]) == 1
    _assert_server_tool(tr["serverTools"][0], toolName="time.now", status="completed", summary="ok")


# ============================================================================================
# Scenario 9 — additivity: legacy ChatResponse fields intact; serverTools defaults to [] when
# the backend reports no server-side tools.
# ============================================================================================
@pytest.mark.asyncio
async def test_additivity_legacy_fields_intact_and_default_empty(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [fake_anthropic.text_result("plain")]
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "hi", "mode": "credits"},
        headers=auth_headers(uid),
    )
    body = r.json()
    # All legacy ChatResponse fields still present (additive change).
    assert set(body.keys()) >= {
        "status",
        "sessionId",
        "messageStepId",
        "stepId",
        "assistantMessage",
        "toolCalls",
        "toolCall",
        "blockReason",
        "usage",
        "serverTools",
    }
    # Default is [] (a list, never null/absent).
    assert body["serverTools"] == []
    assert isinstance(body["serverTools"], list)
