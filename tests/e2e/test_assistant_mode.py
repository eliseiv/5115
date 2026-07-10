"""E2E for assistant_mode (ADR-012) + auto-title (chats/03) through /v1/chat/run.

assistantMode is optional: resolution order is explicit request -> preferences default ->
'chat'. It is fixed on the session at creation and ignored when resuming. It selects the
system prompt only — it never changes billing (mode/credits stay independent). The session
title is auto-generated from the first user message.

Anthropic is faked at the client boundary; DB is the real container.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import FakeAnthropicClient, auth_headers, seed_user


async def _debits(maker: async_sessionmaker[AsyncSession], uid: uuid.UUID) -> int:
    async with maker() as s:
        n = await s.scalar(
            text("SELECT count(*) FROM ledger_transactions WHERE user_id=:u AND type='debit'"),
            {"u": str(uid)},
        )
        return int(n or 0)


@pytest.mark.asyncio
async def test_explicit_assistant_mode_selects_code_prompt_without_changing_billing(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [fake_anthropic.text_result("ok")]
    r = await client.post(
        "/v1/chat/run",
        json={
            "userId": str(uid),
            "projectId": "p",
            "message": "refactor this function",
            "mode": "credits",
            "assistantMode": "code",
        },
        headers=auth_headers(uid),
    )
    assert r.json()["status"] == "assistant_message"
    # assistant_mode=code → code system prompt selected.
    assert "coding assistant" in fake_anthropic.calls[-1]["system_prompt"]
    # billing is independent of assistant_mode: credits mode still debits exactly once.
    assert await _debits(db_sessionmaker, uid) == 1
    # session stored with assistant_mode=code.
    async with db_sessionmaker() as s:
        am = await s.scalar(
            text("SELECT assistant_mode FROM chat_sessions WHERE user_id=:u"), {"u": str(uid)}
        )
    assert am == "code"


@pytest.mark.asyncio
async def test_absent_assistant_mode_falls_back_to_preferences_default(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    # set the user's default to 'code' via preferences.
    await client.patch(
        "/v1/preferences", json={"defaultAssistantMode": "code"}, headers=auth_headers(uid)
    )
    fake_anthropic.responses = [fake_anthropic.text_result("ok")]
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "projectId": "p", "message": "hello", "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert r.json()["status"] == "assistant_message"
    # no assistantMode in request → preference default 'code' applied.
    assert "coding assistant" in fake_anthropic.calls[-1]["system_prompt"]


@pytest.mark.asyncio
async def test_absent_assistant_mode_and_no_preferences_defaults_to_chat(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [fake_anthropic.text_result("ok")]
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "projectId": "p", "message": "hello", "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert r.json()["status"] == "assistant_message"
    # no request override + no preferences row → 'chat' default.
    assert "helpful assistant" in fake_anthropic.calls[-1]["system_prompt"]


@pytest.mark.asyncio
async def test_auto_title_from_first_user_message(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [fake_anthropic.text_result("ok")]
    first_message = "Plan my trip to Lisbon"
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "projectId": "p", "message": first_message, "mode": "credits"},
        headers=auth_headers(uid),
    )
    sid = r.json()["sessionId"]
    # the new session got an auto-generated title derived from the first user message.
    async with db_sessionmaker() as s:
        title = await s.scalar(text("SELECT title FROM chat_sessions WHERE id=:s"), {"s": sid})
    assert title is not None
    assert title != ""
    # title derives from the message content.
    assert "Lisbon" in title or first_message.startswith(title[:10])
