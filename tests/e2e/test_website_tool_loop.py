"""E2E: server-side site.* tool-loop through /chat/run (ADR-011, website-builder/09-testing.md).

FakeAnthropic scripts site.* tool_use turns; the backend executes them synchronously inside the
loop WITHOUT a round-trip to iOS, persists site_files, and bills exactly 1 credit on the final
assistant_message. DB is the real container.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from tests.conftest import FakeAnthropicClient, auth_headers, seed_user
from tests.fake_client_tool import (
    FAKE_CLIENT_TOOL,
    FAKE_CLIENT_TOOL_WIRE,
    register_fake_client_tool,
)


@pytest.fixture
def preview_secret() -> AsyncIterator[None]:
    settings = get_settings()
    orig = settings.preview_url_secret
    settings.preview_url_secret = "loop-secret-0123456789abcdef0123456789abcdef0123"
    yield
    settings.preview_url_secret = orig


async def _count(maker: async_sessionmaker[AsyncSession], sql: str, uid: uuid.UUID) -> int:
    async with maker() as s:
        return int(await s.scalar(text(sql), {"u": str(uid)}) or 0)


@pytest.mark.asyncio
async def test_server_side_site_write_loop_no_round_trip_single_debit(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    preview_secret: None,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)

    # Claude: write index.html (server-side) → ask for preview (server-side) → final message.
    fake_anthropic.responses = [
        fake_anthropic.tool_result(
            "site.write_file",
            {
                "path": "index.html",
                "content": "<h1>Landing</h1>",
                "contentType": "text/html",
                "encoding": "utf8",
            },
        ),
        fake_anthropic.tool_result("site.preview", {"entry": "index.html"}),
        fake_anthropic.text_result("Your landing page is ready."),
    ]

    r = await client.post(
        "/v1/chat/run",
        json={
            "userId": str(uid),
            "projectId": "site-proj",
            "message": "make a landing",
            "mode": "credits",
        },
        headers=auth_headers(uid),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # The whole site.* loop ran on the backend: the client sees the FINAL assistant_message,
    # never a status=tool_call (no round-trip to iOS for server-side tools).
    assert body["status"] == "assistant_message"
    assert body["assistantMessage"] == "Your landing page is ready."

    # site_files persisted under the session's project (IDOR: project from session, not args).
    async with db_sessionmaker() as s:
        files = await s.scalar(text("SELECT count(*) FROM site_files"))
        proj_owner = await s.scalar(text("SELECT user_id FROM projects LIMIT 1"))
        ext = await s.scalar(text("SELECT external_project_id FROM projects LIMIT 1"))
    assert int(files) == 1
    assert str(proj_owner) == str(uid)
    assert ext == "site-proj"

    # Exactly ONE debit despite 3 Anthropic calls and 2 server-side tool rounds (ADR-006).
    debits = await _count(
        db_sessionmaker,
        "SELECT count(*) FROM ledger_transactions WHERE user_id=:u AND type='debit'",
        uid,
    )
    assert debits == 1

    # site.write_file recorded a tool_mutation audit (AC-7 server-side branch).
    muts = await _count(
        db_sessionmaker,
        "SELECT count(*) FROM audit_logs WHERE user_id=:u AND event_type='tool_mutation'",
        uid,
    )
    assert muts == 1


@pytest.mark.asyncio
async def test_site_tools_do_not_charge_extra_credits(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    preview_secret: None,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=10)
    # Three site.write_file rounds then final message → still exactly 1 credit.
    fake_anthropic.responses = [
        fake_anthropic.tool_result(
            "site.write_file",
            {
                "path": f"p{i}.html",
                "content": "<p>x</p>",
                "contentType": "text/html",
                "encoding": "utf8",
            },
        )
        for i in range(3)
    ] + [fake_anthropic.text_result("done")]

    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "projectId": "multi", "message": "build", "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert r.json()["status"] == "assistant_message"
    async with db_sessionmaker() as s:
        bal = await s.scalar(text("SELECT balance FROM wallets WHERE user_id=:u"), {"u": str(uid)})
    assert int(bal) == 9  # 10 - 1 (billing = 1 credit per assistant message, not per site.*)


@pytest.mark.asyncio
async def test_max_server_tool_rounds_guard_502(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    preview_secret: None,
) -> None:
    settings = get_settings()
    orig = settings.max_server_tool_rounds
    settings.max_server_tool_rounds = 2
    try:
        async with db_sessionmaker() as s:
            uid = await seed_user(s, subscription="active", balance=5)
        # Always return a server-side tool → never reaches a final assistant_message.
        fake_anthropic.responses = [
            fake_anthropic.tool_result(
                "site.write_file",
                {
                    "path": f"f{i}.html",
                    "content": "x",
                    "contentType": "text/html",
                    "encoding": "utf8",
                },
            )
            for i in range(10)
        ]
        r = await client.post(
            "/v1/chat/run",
            json={"userId": str(uid), "projectId": "loop", "message": "loop", "mode": "credits"},
            headers=auth_headers(uid),
        )
        # Controlled failure, never an infinite loop, and NO billing (no final assistant message).
        assert r.status_code == 502
        debits = await _count(
            db_sessionmaker,
            "SELECT count(*) FROM ledger_transactions WHERE user_id=:u AND type='debit'",
            uid,
        )
        assert debits == 0
    finally:
        settings.max_server_tool_rounds = orig


@pytest.mark.asyncio
async def test_mixed_server_and_client_tools_hands_off_client_side(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    preview_secret: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn with a client-side tool returns status=tool_call; server-side ran on backend."""
    register_fake_client_tool(monkeypatch)  # ADR-063: test-only client-side example tool
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)

    # One turn emits BOTH a server-side site.write_file AND a client-side tool.
    server_block = {
        "type": "tool_use",
        "id": "toolu_server1",
        "name": "site_write_file",
        "input": {
            "path": "index.html",
            "content": "<h1>x</h1>",
            "contentType": "text/html",
            "encoding": "utf8",
        },
    }
    client_block = {
        "type": "tool_use",
        "id": "toolu_client1",
        "name": FAKE_CLIENT_TOOL_WIRE,
        "input": {"path": "a.txt"},
    }
    from app.chat.anthropic_client import AnthropicResult, AnthropicUsage

    usage = AnthropicUsage(
        input_tokens=1,
        output_tokens=1,
        model="claude-sonnet-4-5",
        cache_read_tokens=0,
        cache_write_tokens=0,
    )
    mixed = AnthropicResult(
        stop_reason="tool_use",
        content_blocks=[server_block, client_block],
        usage=usage,
        text="",
        tool_uses=[
            {"id": "toolu_server1", "name": "site.write_file", "input": server_block["input"]},
            {"id": "toolu_client1", "name": FAKE_CLIENT_TOOL, "input": client_block["input"]},
        ],
    )
    fake_anthropic.responses = [mixed]

    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "projectId": "mixed", "message": "go", "mode": "credits"},
        headers=auth_headers(uid),
    )
    body = r.json()
    # Client-side tool is handed off to iOS.
    assert body["status"] == "tool_call"
    assert body["toolCall"]["name"] == FAKE_CLIENT_TOOL
    # Server-side write executed on the backend already (file persisted, mutation audited).
    async with db_sessionmaker() as s:
        files = await s.scalar(text("SELECT count(*) FROM site_files"))
    assert int(files) == 1
    # No debit yet (no final assistant_message on this step).
    debits = await _count(
        db_sessionmaker,
        "SELECT count(*) FROM ledger_transactions WHERE user_id=:u AND type='debit'",
        uid,
    )
    assert debits == 0
