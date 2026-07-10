"""Integration tests for ADR-037 — `ChatRunRequest.context` injection into the turn-0 user message.

Real PostgreSQL container; the LLM client is faked at the create_message boundary (conftest
`FakeAnthropicClient`), which records the WIRE view AND the neutral kwargs handed by the
orchestrator. ADR-037 injection happens in `orchestrator.run()` BEFORE the provider client, so we
assert on (a) the actual turn-0 user content that reached create_message and (b) the persisted
user-step payload (correct replay), plus the prompt-cache / continuation invariants.

Covers (follow_up_for_qa):
1. backward compatibility (no context / empty / no valid keys → user text == bare message);
2. happy path (exact block + "\\n\\n" + message as the first user text block);
3. lenient per-key validation end-to-end → NOT 422; invalid keys dropped, valid kept;
4. size limit (> size_limit_context) → 422 at the schema layer (no upstream, no session);
5. continuation: context is NOT re-injected on /chat/tool-result (block only in turn-0 history);
6. system-prompt invariant: context does NOT change `system` (prompt cache intact);
7. provider-agnostic: identical injection on provider=openai (persisted user-step has the block).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from tests.conftest import FakeAnthropicClient, auth_headers, seed_user

_BLOCK_PREFIX = "[Conversation settings for this message:"


def _turn0_user_text(fake: FakeAnthropicClient, call_index: int = 0) -> str:
    """Extract the first user text block of the given create_message call (wire view)."""
    msgs = fake.calls[call_index]["messages"]
    user0 = next(m for m in msgs if m.get("role") == "user")
    content = user0["content"]
    assert isinstance(content, list), content
    first_text = next(b for b in content if isinstance(b, dict) and b.get("type") == "text")
    return str(first_text["text"])


async def _user_step_text(maker: async_sessionmaker[AsyncSession], session_id: str) -> str:
    """The persisted turn-0 user-step text block (first user step by seq)."""
    async with maker() as s:
        payload = await s.scalar(
            text(
                "SELECT payload FROM chat_steps WHERE session_id=:sid AND role='user' "
                "ORDER BY seq LIMIT 1"
            ),
            {"sid": session_id},
        )
    assert payload is not None
    for block in payload["content"]:
        if block.get("type") == "text":
            return str(block["text"])
    raise AssertionError("no text block in persisted user step")


# ----------------------------- backward compatibility -----------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "context",
    [None, {}, {"unknownKey": "x"}, {"responseStyle": "not-an-enum"}],
)
async def test_no_valid_context_user_text_is_bare_message(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    context: dict[str, Any] | None,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [fake_anthropic.text_result("ok")]
    payload: dict[str, Any] = {"userId": str(uid), "message": "hello world", "mode": "credits"}
    if context is not None:
        payload["context"] = context

    r = await client.post("/v1/chat/run", json=payload, headers=auth_headers(uid))
    assert r.status_code == 200, r.text
    # No block prepended → the turn-0 user text equals the bare message (wire + persisted).
    assert _turn0_user_text(fake_anthropic) == "hello world"
    assert await _user_step_text(db_sessionmaker, r.json()["sessionId"]) == "hello world"


# ----------------------------- happy path -----------------------------
@pytest.mark.asyncio
async def test_happy_path_block_prepended_to_turn0(
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
            "message": "write a sort",
            "mode": "credits",
            "context": {
                "codeLanguage": "Swift",
                "responseStyle": "concise",
                "locale": "ru-RU",
            },
        },
        headers=auth_headers(uid),
    )
    assert r.status_code == 200, r.text
    expected = (
        "[Conversation settings for this message: "
        "codeLanguage=Swift; responseStyle=concise; locale=ru-RU]\n\nwrite a sort"
    )
    # The block leads, then "\n\n", then the message — both on the wire and persisted.
    assert _turn0_user_text(fake_anthropic) == expected
    assert await _user_step_text(db_sessionmaker, r.json()["sessionId"]) == expected


@pytest.mark.asyncio
async def test_deterministic_order_via_endpoint(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """Scrambled input key order → fixed render order in the injected block."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [fake_anthropic.text_result("ok")]

    r = await client.post(
        "/v1/chat/run",
        json={
            "userId": str(uid),
            "message": "m",
            "mode": "credits",
            "context": {
                "locale": "en",
                "tone": "friendly",
                "verbosity": "high",
                "responseStyle": "detailed",
                "codeLanguage": "Python",
            },
        },
        headers=auth_headers(uid),
    )
    assert r.status_code == 200, r.text
    text0 = _turn0_user_text(fake_anthropic)
    assert text0.startswith(
        "[Conversation settings for this message: "
        "codeLanguage=Python; responseStyle=detailed; verbosity=high; tone=friendly; locale=en]"
    )


# ----------------------------- lenient per-key (NOT 422) -----------------------------
@pytest.mark.asyncio
async def test_partial_invalid_context_is_not_422_drops_invalid(
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
            "message": "go",
            "mode": "credits",
            "context": {
                "responseStyle": "verbose",  # out-of-enum → dropped
                "verbosity": 99,  # wrong type → dropped
                "codeLanguage": "x" * 50,  # too long → dropped
                "locale": "ru RU!",  # bad charset → dropped
                "tone": "formal",  # valid → kept
                "futureKey": "ignored",  # unknown → ignored
            },
        },
        headers=auth_headers(uid),
    )
    # Lenient: a partially-invalid context is NOT a 422; valid keys applied, invalid dropped.
    assert r.status_code == 200, r.text
    assert _turn0_user_text(fake_anthropic) == (
        "[Conversation settings for this message: tone=formal]\n\ngo"
    )


@pytest.mark.asyncio
async def test_all_invalid_context_is_not_422_bare_message(
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
            "message": "only",
            "mode": "credits",
            "context": {"responseStyle": "nope", "verbosity": "nope"},
        },
        headers=auth_headers(uid),
    )
    assert r.status_code == 200, r.text  # no survivors → still NOT 422
    assert _turn0_user_text(fake_anthropic) == "only"


@pytest.mark.asyncio
async def test_escaping_value_with_delimiters_does_not_break_block(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """A free-string value with `;`/`=`/newline cannot inject extra pairs into the block."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [fake_anthropic.text_result("ok")]
    r = await client.post(
        "/v1/chat/run",
        json={
            "userId": str(uid),
            "message": "m",
            "mode": "credits",
            "context": {"tone": "friendly; locale=evil\ninjected", "codeLanguage": "Swift"},
        },
        headers=auth_headers(uid),
    )
    assert r.status_code == 200, r.text
    text0 = _turn0_user_text(fake_anthropic)
    block = text0.split("\n\n", 1)[0]
    # Inside the block (before the "\n\n" message separator) there must be no smuggled newline,
    # and the smuggled "locale=" did not become a real second locale pair.
    assert "\n" not in block
    pairs = block[len(_BLOCK_PREFIX) : -1].strip().split("; ")
    keys = [p.split("=", 1)[0].strip() for p in pairs]
    assert keys == ["codeLanguage", "tone"]


# ----------------------------- size limit (schema 422) -----------------------------
@pytest.mark.asyncio
async def test_oversize_context_is_422_no_upstream_no_session(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    # A serialized context above size_limit_context (default 64KB) → schema 422 pre-orchestration.
    big = "z" * (get_settings().size_limit_context + 100)
    r = await client.post(
        "/v1/chat/run",
        json={
            "userId": str(uid),
            "message": "m",
            "mode": "credits",
            "context": {"blob": big},
        },
        headers=auth_headers(uid),
    )
    assert r.status_code == 422, r.text
    assert not fake_anthropic.calls  # validation runs before any upstream call
    async with db_sessionmaker() as s:
        n = await s.scalar(
            text("SELECT count(*) FROM chat_sessions WHERE user_id=:u"), {"u": str(uid)}
        )
    assert int(n or 0) == 0  # no session persisted


# ----------------------------- continuation: no re-injection -----------------------------
@pytest.mark.asyncio
async def test_context_not_reinjected_on_tool_result(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """The block lives ONLY in the turn-0 user message; the continuation history shows it once."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [
        fake_anthropic.tool_result("files.read", {"path": "a.txt"}),
        fake_anthropic.text_result("done"),
    ]
    r1 = await client.post(
        "/v1/chat/run",
        json={
            "userId": str(uid),
            "projectId": "p",
            "message": "read it",
            "mode": "credits",
            "context": {"locale": "ru-RU"},
        },
        headers=auth_headers(uid),
    )
    b1 = r1.json()
    assert b1["status"] == "tool_call", b1
    sess = b1["sessionId"]
    tcid = b1["toolCall"]["id"]

    r2 = await client.post(
        "/v1/chat/tool-result",
        json={"userId": str(uid), "sessionId": sess, "toolCallId": tcid, "result": {"ok": 1}},
        headers=auth_headers(uid),
    )
    assert r2.json()["status"] == "assistant_message", r2.text

    # The continuation (2nd) call's full message history must contain the block EXACTLY ONCE
    # (the turn-0 user message), never re-injected on the tool-result continuation.
    continuation_messages = fake_anthropic.calls[-1]["messages"]
    blob = json.dumps(continuation_messages, ensure_ascii=False)
    assert blob.count(_BLOCK_PREFIX) == 1, blob

    # And only the FIRST user step carries it; no new user step was created on tool-result.
    async with db_sessionmaker() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT payload FROM chat_steps WHERE session_id=:sid AND role='user' "
                    "ORDER BY seq"
                ),
                {"sid": sess},
            )
        ).all()
    user_payloads = json.dumps([row[0] for row in rows], ensure_ascii=False)
    assert user_payloads.count(_BLOCK_PREFIX) == 1, user_payloads


# ----------------------------- system-prompt invariant (prompt cache) -----------------------------
@pytest.mark.asyncio
async def test_context_does_not_change_system_prompt(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """`system` is identical with and without context → prompt cache (ephemeral) stays intact."""
    async with db_sessionmaker() as s:
        uid_a = await seed_user(s, subscription="active", balance=5)
        uid_b = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [
        fake_anthropic.text_result("a"),
        fake_anthropic.text_result("b"),
    ]
    # Same assistantMode (chat) so the base system prompt is comparable; only context differs.
    r_no = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid_a), "message": "m", "mode": "credits", "assistantMode": "chat"},
        headers=auth_headers(uid_a),
    )
    assert r_no.status_code == 200, r_no.text
    system_no_ctx = fake_anthropic.calls[-1]["system_prompt"]

    r_ctx = await client.post(
        "/v1/chat/run",
        json={
            "userId": str(uid_b),
            "message": "m",
            "mode": "credits",
            "assistantMode": "chat",
            "context": {"codeLanguage": "Swift", "responseStyle": "concise"},
        },
        headers=auth_headers(uid_b),
    )
    assert r_ctx.status_code == 200, r_ctx.text
    system_with_ctx = fake_anthropic.calls[-1]["system_prompt"]

    assert system_no_ctx == system_with_ctx
    # The block must NOT have leaked into the system prompt (it belongs to user content only).
    assert _BLOCK_PREFIX not in (system_with_ctx or "")


# ----------------------------- provider-agnostic -----------------------------
@pytest.fixture
def restore_provider() -> Iterator[None]:
    s = get_settings()
    orig = s.llm_provider
    yield
    s.llm_provider = orig


@pytest.mark.asyncio
async def test_injection_is_provider_agnostic_openai(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    restore_provider: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With provider=openai the orchestrator still injects the block into user content (turn-0).

    Injection happens in `orchestrator.run()` BEFORE the provider client, so it is identical for
    both providers. The persisted user-step payload (provider-agnostic, replayed to either provider)
    carries the block; this is the single source the active client serializes per provider.

    Hermetic: with provider=openai the `get_llm_client` factory would otherwise build a REAL
    `OpenAIClient` (its `create_message` hits the OpenAI network → 401 under a placeholder key in
    CI). We mirror conftest's anthropic-singleton patch on the OpenAI seam: pin
    `llm_client._openai_singleton` to the same faithful `LLMClient` double (`fake_anthropic`) so the
    factory returns the fake on the openai path — no `OpenAIClient()` construction, no network. The
    assertion is on the PROVIDER-INDEPENDENT persisted user-step (the injected block), so the fake's
    Anthropic-shaped wire recording is irrelevant here. `monkeypatch` auto-restores the singleton.
    """
    from app.chat import llm_client as llm_client_mod

    get_settings().llm_provider = "openai"
    monkeypatch.setattr(llm_client_mod, "_openai_singleton", fake_anthropic, raising=False)
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [fake_anthropic.text_result("ok")]

    r = await client.post(
        "/v1/chat/run",
        json={
            "userId": str(uid),
            "message": "translate",
            "mode": "credits",
            "context": {"locale": "de-DE", "responseStyle": "balanced"},
        },
        headers=auth_headers(uid),
    )
    assert r.status_code == 200, r.text
    expected = (
        "[Conversation settings for this message: responseStyle=balanced; locale=de-DE]"
        "\n\ntranslate"
    )
    # Persisted user content (provider-independent) carries exactly the injected block + message.
    assert await _user_step_text(db_sessionmaker, r.json()["sessionId"]) == expected
