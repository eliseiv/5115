"""Integration tests for ADR-022: optional projectId + site.* gating by project presence.

Real PostgreSQL container, Anthropic faked at the client boundary. Covers (per the backend
follow_up scenarios):
1. Contract: /chat/run without projectId creates a session (project_id NULL) and answers normally;
   blank projectId → 422 and no session.
2. Axis-A gating: no-project session → tools to Anthropic exclude site.* but keep client-side;
   project session → full 13-tool set. Checked on both /chat/run and /chat/tool-result.
3. Resume session-fixed: session created with project A; resume body projectId=B → A is used
   (request field ignored, not an error); resume without projectId keeps site.* offered.
4. Defensive-guard: fake Anthropic returns a site.* tool_use on a project-less session → 502
   UpstreamError, site.* NOT executed, no project resolved.
5. Website-builder regression: project session offers + executes site.* server-side.
6. Billing: 1 credit = 1 message, with and without projectId.

Axis B (assistant_mode) is NOT asserted as done (Q-012-1 Open): the only gate is project_id.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.chat.anthropic_client import AnthropicResult, AnthropicUsage
from app.chat.tools import SERVER_SIDE_TOOLS, to_domain_tool_name
from app.config import get_settings
from tests.conftest import FakeAnthropicClient, auth_headers, seed_user


# --- helpers -------------------------------------------------------------------------------
def _offered_domain_tools(call: dict) -> set[str]:
    """Domain (dotted) names of the tools offered to Anthropic on a recorded create_message call."""
    return {to_domain_tool_name(t["name"]) for t in call["tools"]}


async def _project_id_of(maker: async_sessionmaker[AsyncSession], session_id: str) -> str | None:
    async with maker() as s:
        return await s.scalar(
            text("SELECT project_id FROM chat_sessions WHERE id=:sid"), {"sid": session_id}
        )


async def _count(maker: async_sessionmaker[AsyncSession], sql: str, uid: uuid.UUID) -> int:
    async with maker() as s:
        return int(await s.scalar(text(sql), {"u": str(uid)}) or 0)


@pytest.fixture
def preview_secret() -> object:
    settings = get_settings()
    orig = settings.preview_url_secret
    settings.preview_url_secret = "adr022-secret-0123456789abcdef0123456789abcdef01"
    yield
    settings.preview_url_secret = orig


# ============================ scenario 1: contract ============================
@pytest.mark.asyncio
async def test_run_without_project_id_creates_session_with_null_project(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [fake_anthropic.text_result("clean chat reply")]

    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "hi without project", "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "assistant_message"
    assert body["assistantMessage"] == "clean chat reply"

    # The session was created with project_id = NULL.
    assert await _project_id_of(db_sessionmaker, body["sessionId"]) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("blank", ["", "   "])
async def test_run_blank_project_id_is_422_and_no_session(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    blank: str,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "projectId": blank, "message": "hi", "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert r.status_code == 422, r.text
    # Validator runs before any DB write / Anthropic call: no session, no upstream call.
    assert not fake_anthropic.calls
    sessions = await _count(
        db_sessionmaker, "SELECT count(*) FROM chat_sessions WHERE user_id=:u", uid
    )
    assert sessions == 0


# ============================ scenario 2: axis-A gating ============================
@pytest.mark.asyncio
async def test_no_project_session_does_not_offer_site_tools_on_run(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [fake_anthropic.text_result("ok")]

    await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "hi", "mode": "credits"},
        headers=auth_headers(uid),
    )
    offered = _offered_domain_tools(fake_anthropic.calls[0])
    assert offered.isdisjoint(SERVER_SIDE_TOOLS)  # no site.*
    # client-side tools still offered (axis A does not touch them).
    assert "files.read" in offered
    assert "calendar.read" in offered
    assert "reminders.read" in offered
    # ADR-026: time.now (global server-side) is offered even without a project.
    assert "time.now" in offered
    # ADR-058: image.generate (global server-side) is offered too — its key-gate is satisfied
    # (conftest forces a non-empty OPENAI_API_KEY); the project (axis-A) gate does not touch it.
    assert "image.generate" in offered
    assert len(offered) == 10  # 8 client-side + time.now + image.generate


@pytest.mark.asyncio
async def test_project_session_offers_full_server_side_set_on_run(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [fake_anthropic.text_result("ok")]

    await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "projectId": "proj-1", "message": "hi", "mode": "credits"},
        headers=auth_headers(uid),
    )
    offered = _offered_domain_tools(fake_anthropic.calls[0])
    assert offered >= SERVER_SIDE_TOOLS  # site.* present
    assert "time.now" in offered  # ADR-026: global server-side, always offered
    assert "image.generate" in offered  # ADR-058: global server-side, key-gate satisfied
    # 8 client-side + 5 site.* + time.now + image.generate (quiz.generate is dialog-gated out).
    assert len(offered) == 15


@pytest.mark.asyncio
async def test_gating_holds_across_tool_result_continuation_no_project(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """has_project derives from sess.project_id on /chat/tool-result too → site.* still excluded."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [
        fake_anthropic.tool_result("files.read", {"path": "a.txt"}),
        fake_anthropic.text_result("done"),
    ]
    r1 = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "go", "mode": "credits"},
        headers=auth_headers(uid),
    )
    b1 = r1.json()
    assert b1["status"] == "tool_call"
    await client.post(
        "/v1/chat/tool-result",
        json={
            "userId": str(uid),
            "sessionId": b1["sessionId"],
            "toolCallId": b1["toolCall"]["id"],
            "result": {"ok": True},
        },
        headers=auth_headers(uid),
    )
    # Both the run call and the continuation call must exclude site.*.
    for call in fake_anthropic.calls:
        assert _offered_domain_tools(call).isdisjoint(SERVER_SIDE_TOOLS)


@pytest.mark.asyncio
async def test_gating_holds_across_tool_result_continuation_with_project(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [
        fake_anthropic.tool_result("files.read", {"path": "a.txt"}),
        fake_anthropic.text_result("done"),
    ]
    r1 = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "projectId": "proj-x", "message": "go", "mode": "credits"},
        headers=auth_headers(uid),
    )
    b1 = r1.json()
    await client.post(
        "/v1/chat/tool-result",
        json={
            "userId": str(uid),
            "sessionId": b1["sessionId"],
            "toolCallId": b1["toolCall"]["id"],
            "result": {"ok": True},
        },
        headers=auth_headers(uid),
    )
    for call in fake_anthropic.calls:
        assert _offered_domain_tools(call) >= SERVER_SIDE_TOOLS


# ============================ scenario 3: resume session-fixed ============================
@pytest.mark.asyncio
async def test_resume_with_different_project_id_uses_session_value(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """Session created with project A; resume body projectId=B → A is used (field ignored)."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [
        fake_anthropic.text_result("first"),
        fake_anthropic.text_result("second"),
    ]
    r1 = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "projectId": "A", "message": "one", "mode": "credits"},
        headers=auth_headers(uid),
    )
    sess = r1.json()["sessionId"]

    # Resume same session with a DIFFERENT projectId in the body.
    r2 = await client.post(
        "/v1/chat/run",
        json={
            "userId": str(uid),
            "sessionId": sess,
            "projectId": "B",
            "message": "two",
            "mode": "credits",
        },
        headers=auth_headers(uid),
    )
    assert r2.json()["status"] == "assistant_message"
    # Stored project_id is unchanged (still A): request field is ignored on resume (not an error).
    assert await _project_id_of(db_sessionmaker, sess) == "A"
    # site.* still offered (A is non-null) on the resume call.
    assert _offered_domain_tools(fake_anthropic.calls[-1]) >= SERVER_SIDE_TOOLS


@pytest.mark.asyncio
async def test_resume_without_project_id_for_project_session_keeps_site_tools(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [
        fake_anthropic.text_result("first"),
        fake_anthropic.text_result("second"),
    ]
    r1 = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "projectId": "P", "message": "one", "mode": "credits"},
        headers=auth_headers(uid),
    )
    sess = r1.json()["sessionId"]

    # Resume WITHOUT a projectId (omitted) → session value (P) still governs → site.* offered.
    r2 = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "sessionId": sess, "message": "two", "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert r2.json()["status"] == "assistant_message"
    assert await _project_id_of(db_sessionmaker, sess) == "P"
    assert _offered_domain_tools(fake_anthropic.calls[-1]) >= SERVER_SIDE_TOOLS


@pytest.mark.asyncio
async def test_resume_with_project_id_for_chat_session_keeps_site_tools_excluded(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """Inverse session-fixed: chat-only session (NULL), resume body sends a projectId → ignored,
    session stays project-less and site.* remain excluded."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [
        fake_anthropic.text_result("first"),
        fake_anthropic.text_result("second"),
    ]
    r1 = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "one", "mode": "credits"},
        headers=auth_headers(uid),
    )
    sess = r1.json()["sessionId"]
    assert await _project_id_of(db_sessionmaker, sess) is None

    r2 = await client.post(
        "/v1/chat/run",
        json={
            "userId": str(uid),
            "sessionId": sess,
            "projectId": "late-project",
            "message": "two",
            "mode": "credits",
        },
        headers=auth_headers(uid),
    )
    assert r2.json()["status"] == "assistant_message"
    # Request field ignored on resume: session stays NULL, site.* still excluded.
    assert await _project_id_of(db_sessionmaker, sess) is None
    assert _offered_domain_tools(fake_anthropic.calls[-1]).isdisjoint(SERVER_SIDE_TOOLS)


# ============================ scenario 4: defensive-guard ============================
@pytest.mark.asyncio
async def test_site_tool_use_on_project_less_session_is_502_and_not_executed(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """Even though site.* is not offered, if Claude returns a site.* tool_use on a NULL-project
    session it is an upstream anomaly → UpstreamError (502), nothing executed, no project resolved.
    """
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)

    # Anomalous: a site.write_file tool_use although there is no project.
    usage = AnthropicUsage(
        input_tokens=1,
        output_tokens=1,
        model="claude-sonnet-4-5",
        cache_read_tokens=0,
        cache_write_tokens=0,
    )
    rogue = AnthropicResult(
        stop_reason="tool_use",
        content_blocks=[
            {
                "type": "tool_use",
                "id": "toolu_rogue",
                "name": "site_write_file",
                "input": {
                    "path": "index.html",
                    "content": "<h1>x</h1>",
                    "contentType": "text/html",
                    "encoding": "utf8",
                },
            }
        ],
        usage=usage,
        text="",
        tool_uses=[
            {
                "id": "toolu_rogue",
                "name": "site.write_file",
                "input": {
                    "path": "index.html",
                    "content": "<h1>x</h1>",
                    "contentType": "text/html",
                    "encoding": "utf8",
                },
            }
        ],
    )
    fake_anthropic.responses = [rogue]

    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "make a site", "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert r.status_code == 502, r.text
    # site.* not executed: no project / site_files created, no debit.
    async with db_sessionmaker() as s:
        projects = await s.scalar(text("SELECT count(*) FROM projects"))
        site_files = await s.scalar(text("SELECT count(*) FROM site_files"))
    assert int(projects) == 0
    assert int(site_files) == 0
    debits = await _count(
        db_sessionmaker,
        "SELECT count(*) FROM ledger_transactions WHERE user_id=:u AND type='debit'",
        uid,
    )
    assert debits == 0


# ============================ scenario 5: website-builder regression ============================
@pytest.mark.asyncio
async def test_project_session_executes_site_tools_server_side(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    preview_secret: object,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [
        fake_anthropic.tool_result(
            "site.write_file",
            {
                "path": "index.html",
                "content": "<h1>Hi</h1>",
                "contentType": "text/html",
                "encoding": "utf8",
            },
        ),
        fake_anthropic.text_result("site ready"),
    ]
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "projectId": "regress", "message": "build", "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert r.json()["status"] == "assistant_message"
    # site.* executed server-side: file persisted under the session project, resolved from session.
    async with db_sessionmaker() as s:
        files = await s.scalar(text("SELECT count(*) FROM site_files"))
        ext = await s.scalar(text("SELECT external_project_id FROM projects LIMIT 1"))
        owner = await s.scalar(text("SELECT user_id FROM projects LIMIT 1"))
    assert int(files) == 1
    assert ext == "regress"
    assert str(owner) == str(uid)


# ============================ scenario 6: billing ============================
@pytest.mark.asyncio
@pytest.mark.parametrize("project", [None, "billing-proj"])
async def test_one_credit_per_message_with_and_without_project(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    project: str | None,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [fake_anthropic.text_result("ok")]
    payload: dict[str, object] = {"userId": str(uid), "message": "hi", "mode": "credits"}
    if project is not None:
        payload["projectId"] = project

    r = await client.post("/v1/chat/run", json=payload, headers=auth_headers(uid))
    assert r.json()["status"] == "assistant_message"

    async with db_sessionmaker() as s:
        bal = await s.scalar(text("SELECT balance FROM wallets WHERE user_id=:u"), {"u": str(uid)})
    assert int(bal) == 4  # exactly 1 credit, regardless of project presence
    debits = await _count(
        db_sessionmaker,
        "SELECT count(*) FROM ledger_transactions WHERE user_id=:u AND type='debit'",
        uid,
    )
    assert debits == 1
