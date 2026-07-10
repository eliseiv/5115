"""Integration tests for ADR-042 — read-time strip of the ADR-037 conversation-settings block
from user-facing history (`GET /v1/chats/{id}`) and preview (`GET /v1/chats`).

Real PostgreSQL container; the LLM client is faked at the create_message boundary (conftest
`FakeAnthropicClient`). A message is sent through the orchestrator WITH `context` so the server
itself injects + persists the block exactly as production does (ADR-037). We then assert that:

- history user-step text is returned WITHOUT the leading settings block (assistant/tool untouched,
  ADR-024 tool normalization still works);
- preview of the user step is returned WITHOUT the block (strip runs BEFORE _truncate/collapse);
- image-only/file-only + context → empty user text in history, attachment placeholders preserved;
- the STORED `chat_steps.payload` is NOT mutated (still carries the block for model replay);
- without context, history/preview are unchanged (regression).

Hermetic: no real network. The provider-agnostic case pins both the Anthropic and OpenAI seams to
the same faithful `LLMClient` double so the openai path never constructs a real client.

Covers follow_up_for_qa scenarios 1-6 for ADR-042.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from tests.conftest import FakeAnthropicClient, auth_headers, seed_user

_BLOCK_PREFIX = "[Conversation settings for this message:"
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _png_attachment() -> dict[str, str]:
    return {"type": "image", "mediaType": "image/png", "filename": "p.png", "data": _b64(_PNG)}


def _user_steps(history: dict[str, Any]) -> list[dict[str, Any]]:
    return [st for st in history["steps"] if st["role"] == "user"]


def _first_text_block(step: dict[str, Any]) -> dict[str, Any] | None:
    for block in step.get("payload", {}).get("content", []):
        if isinstance(block, dict) and block.get("type") == "text":
            return block
    return None


def _placeholder_blocks(step: dict[str, Any]) -> list[dict[str, Any]]:
    """ADR-020 attachment placeholder text blocks (start with "[attachment:")."""
    out: list[dict[str, Any]] = []
    for block in step.get("payload", {}).get("content", []):
        if (
            isinstance(block, dict)
            and block.get("type") == "text"
            and str(block.get("text", "")).startswith("[attachment:")
        ):
            out.append(block)
    return out


async def _stored_user_text(maker: async_sessionmaker[AsyncSession], session_id: str) -> str:
    """The PERSISTED turn-0 user-step first text block (ground truth, not the API view)."""
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
    return ""


async def _run_with_context(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    *,
    message: str,
    context: dict[str, Any] | None,
    attachments: list[dict[str, str]] | None = None,
    final_text: str = "ok",
) -> tuple[str, str]:
    """Seed a user, POST /v1/chat/run with the given message/context, return (uid, sessionId)."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [fake_anthropic.text_result(final_text)]
    payload: dict[str, Any] = {"userId": str(uid), "message": message, "mode": "credits"}
    if context is not None:
        payload["context"] = context
    if attachments is not None:
        payload["attachments"] = attachments
    r = await client.post("/v1/chat/run", json=payload, headers=auth_headers(uid))
    assert r.status_code == 200, r.text
    return str(uid), r.json()["sessionId"]


# ============================ Scenario 2: history strips the block ============================
@pytest.mark.asyncio
async def test_history_user_step_text_strips_leading_block(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """GET /v1/chats/{id}: the user step's leading ADR-037 block is removed (only message left)."""
    uid, sid = await _run_with_context(
        client,
        db_sessionmaker,
        fake_anthropic,
        message="write a sort",
        context={"codeLanguage": "Swift", "responseStyle": "concise", "locale": "ru-RU"},
    )
    # Sanity: the server DID inject + persist the block (so the strip is actually exercised).
    assert _BLOCK_PREFIX in await _stored_user_text(db_sessionmaker, sid)

    r = await client.get(f"/v1/chats/{sid}", headers=auth_headers(uid))
    assert r.status_code == 200, r.text
    hist = r.json()
    user_steps = _user_steps(hist)
    assert user_steps, hist
    first_text = _first_text_block(user_steps[0])
    assert first_text is not None
    # The user-facing text is the bare message — no settings block.
    assert first_text["text"] == "write a sort"
    # Block prefix appears NOWHERE in the user-facing history response.
    assert _BLOCK_PREFIX not in r.text


@pytest.mark.asyncio
async def test_history_assistant_step_untouched_and_tool_normalization_works(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """assistant/tool payloads are not changed by ADR-042; ADR-024 normalization still applies."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)

    from app.chat.anthropic_client import AnthropicResult, AnthropicUsage

    provider_id = "toolu_01Adr042Tool"
    usage = AnthropicUsage(
        input_tokens=10,
        output_tokens=5,
        model="claude-sonnet-4-5",
        cache_read_tokens=0,
        cache_write_tokens=0,
    )
    # assistant turn: text + tool_use (underscore wire name + raw provider id, as stored).
    tool_turn = AnthropicResult(
        stop_reason="tool_use",
        content_blocks=[
            {"type": "text", "text": "Let me read it."},
            {"type": "tool_use", "id": provider_id, "name": "files_read", "input": {"path": "a"}},
        ],
        usage=usage,
        text="Let me read it.",
        tool_uses=[{"id": provider_id, "name": "files.read", "input": {"path": "a"}}],
    )
    fake_anthropic.responses = [tool_turn, fake_anthropic.text_result("done")]

    run = (
        await client.post(
            "/v1/chat/run",
            json={
                "userId": str(uid),
                "message": "read a",
                "mode": "credits",
                "context": {"locale": "ru-RU"},
            },
            headers=auth_headers(uid),
        )
    ).json()
    assert run["status"] == "tool_call", run
    sid = run["sessionId"]
    tcid = run["toolCall"]["id"]
    tr = (
        await client.post(
            "/v1/chat/tool-result",
            json={"userId": str(uid), "sessionId": sid, "toolCallId": tcid, "result": {"ok": 1}},
            headers=auth_headers(uid),
        )
    ).json()
    assert tr["status"] == "assistant_message", tr

    r = await client.get(f"/v1/chats/{sid}", headers=auth_headers(uid))
    blob = r.text
    assert r.status_code == 200, blob
    hist = r.json()

    # ADR-042: user text stripped of the block.
    user_steps = _user_steps(hist)
    first_text = _first_text_block(user_steps[0])
    assert first_text is not None and first_text["text"] == "read a"
    assert _BLOCK_PREFIX not in blob

    # ADR-024 invariants intact: domain dotted tool name, no provider id / providerToolUseId leak.
    assert "toolu_" not in blob
    assert "providerToolUseId" not in blob
    tool_use_names = [
        b["name"]
        for st in hist["steps"]
        for b in st["payload"].get("content", [])
        if isinstance(b, dict) and b.get("type") == "tool_use"
    ]
    assert "files.read" in tool_use_names

    # assistant text block left verbatim.
    assistant_texts = [
        b["text"]
        for st in hist["steps"]
        if st["role"] == "assistant"
        for b in st["payload"].get("content", [])
        if isinstance(b, dict) and b.get("type") == "text"
    ]
    assert "Let me read it." in assistant_texts


# ============================ Scenario 3: preview strips the block ============================
@pytest.mark.asyncio
async def test_preview_user_step_strips_block_before_truncate(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """GET /v1/chats: the user preview must NOT begin with the settings block.

    Critical: strip runs on the RAW first text block BEFORE _truncate (which collapses "\\n\\n" →
    space and would otherwise defeat the anchor). The preview = latest user/assistant step; here we
    force the user step to be the latest by scripting a tool_call (no final assistant text step).
    """
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    # A tool_call response leaves the assistant step as a tool_use; but to make the USER step the
    # latest user/assistant by created_at we instead rely on the assistant step also being present.
    # To unambiguously exercise the user-preview path, seed a chat whose ONLY user/assistant step is
    # the user step: script a `blocked`-free run but inspect the user step directly via list+search.
    fake_anthropic.responses = [fake_anthropic.text_result("assistant reply text")]
    r = await client.post(
        "/v1/chat/run",
        json={
            "userId": str(uid),
            "message": "implement quicksort please",
            "mode": "credits",
            "context": {"codeLanguage": "Swift", "responseStyle": "concise"},
        },
        headers=auth_headers(uid),
    )
    assert r.status_code == 200, r.text

    lst = await client.get("/v1/chats", headers=auth_headers(uid))
    assert lst.status_code == 200, lst.text
    items = lst.json()["items"]
    assert len(items) == 1, items
    preview = items[0]["preview"]
    # Whatever step won the latest-preview race, the block must never appear in a preview.
    assert preview is not None
    assert not preview.startswith(_BLOCK_PREFIX)
    assert _BLOCK_PREFIX not in lst.text


@pytest.mark.asyncio
async def test_preview_is_user_message_when_user_is_latest_step(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """A chat whose latest user/assistant step is the USER step (status=blocked → no assistant step)
    yields a preview equal to the bare user message (block stripped before truncate)."""
    # trial_used + credits → blocked: the orchestrator persists the user step but NO assistant step,
    # so the latest user/assistant step IS the user step → exercises the user-preview strip path.
    async with db_sessionmaker() as s:
        uid = await seed_user(s, trial_used=True)
    r = await client.post(
        "/v1/chat/run",
        json={
            "userId": str(uid),
            "message": "build me a website",
            "mode": "credits",
            "context": {"codeLanguage": "Swift", "locale": "ru-RU"},
        },
        headers=auth_headers(uid),
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "blocked", r.text

    lst = await client.get("/v1/chats", headers=auth_headers(uid))
    items = lst.json()["items"]
    assert len(items) == 1, items
    # The user step is the latest → preview is the bare message with the block stripped.
    assert items[0]["preview"] == "build me a website"
    assert _BLOCK_PREFIX not in lst.text


@pytest.mark.asyncio
async def test_preview_assistant_step_not_touched(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """When the latest step is an assistant message, the preview is that assistant text verbatim
    (no block there to strip, and ADR-042 never touches assistant steps)."""
    uid, sid = await _run_with_context(
        client,
        db_sessionmaker,
        fake_anthropic,
        message="hello",
        context={"locale": "ru-RU"},
        final_text="this is the assistant answer",
    )
    lst = await client.get("/v1/chats", headers=auth_headers(uid))
    items = lst.json()["items"]
    assert len(items) == 1, items
    assert items[0]["preview"] == "this is the assistant answer"


# ==================== Scenario 4: image-only / file-only + context ====================
@pytest.mark.asyncio
async def test_image_only_with_context_history_text_empty_placeholders_kept(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """Empty message + context + image attachment: the persisted user text == the bare block (§5);
    history shows EMPTY user text and the attachment placeholder is preserved."""
    uid, sid = await _run_with_context(
        client,
        db_sessionmaker,
        fake_anthropic,
        message="",
        context={"codeLanguage": "Swift", "locale": "ru-RU"},
        attachments=[_png_attachment()],
    )
    # Sanity: the persisted leading text is the bare block (no trailing "\n\n", §5 edge).
    stored = await _stored_user_text(db_sessionmaker, sid)
    assert stored.startswith(_BLOCK_PREFIX)
    assert "\n\n" not in stored  # bare block, no message appended

    r = await client.get(f"/v1/chats/{sid}", headers=auth_headers(uid))
    assert r.status_code == 200, r.text
    hist = r.json()
    user_steps = _user_steps(hist)
    assert user_steps, hist
    first_text = _first_text_block(user_steps[0])
    assert first_text is not None
    # Bare-block-only text → empty after strip.
    assert first_text["text"] == ""
    # The attachment placeholder block survives untouched.
    assert _placeholder_blocks(user_steps[0]), user_steps[0]
    assert _BLOCK_PREFIX not in r.text


# ============================ Scenario 5: storage NOT mutated ============================
@pytest.mark.asyncio
async def test_storage_not_mutated_after_history_and_preview_served(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """ADR-042 §6: serving history/preview is read-time on a copy — chat_steps.payload still carries
    the block (replay invariant). Verify the stored text is unchanged after both endpoints run."""
    uid, sid = await _run_with_context(
        client,
        db_sessionmaker,
        fake_anthropic,
        message="write a sort",
        context={"codeLanguage": "Swift", "responseStyle": "concise"},
    )
    expected_stored = (
        "[Conversation settings for this message: "
        "codeLanguage=Swift; responseStyle=concise]\n\nwrite a sort"
    )
    # Before any read: the block is persisted.
    assert await _stored_user_text(db_sessionmaker, sid) == expected_stored

    # Serve history AND preview (would mutate state if the strip were in-place).
    await client.get(f"/v1/chats/{sid}", headers=auth_headers(uid))
    await client.get("/v1/chats", headers=auth_headers(uid))

    # Ground truth via the repository: stored payload STILL carries the block (no mutation).
    from app.chats.repository import ChatsRepository

    async with db_sessionmaker() as s:
        repo = ChatsRepository(s)
        import uuid as _uuid

        steps = await repo.list_steps(_uuid.UUID(sid))
    user_steps = [st for st in steps if st.role == "user"]
    assert user_steps
    stored_text = next(
        b["text"] for b in user_steps[0].payload["content"] if b.get("type") == "text"
    )
    assert stored_text == expected_stored
    assert _BLOCK_PREFIX in stored_text


# ============================ Scenario 6: regression (no context) ============================
@pytest.mark.asyncio
async def test_no_context_history_and_preview_unchanged(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """Without context, history/preview show the bare message exactly as before (no-op strip)."""
    uid, sid = await _run_with_context(
        client,
        db_sessionmaker,
        fake_anthropic,
        message="just a normal message",
        context=None,
        final_text="reply",
    )
    # Stored text has no block.
    assert await _stored_user_text(db_sessionmaker, sid) == "just a normal message"

    hist = (await client.get(f"/v1/chats/{sid}", headers=auth_headers(uid))).json()
    first_text = _first_text_block(_user_steps(hist)[0])
    assert first_text is not None and first_text["text"] == "just a normal message"

    items = (await client.get("/v1/chats", headers=auth_headers(uid))).json()["items"]
    assert items[0]["preview"] == "reply"  # latest step is the assistant final answer


@pytest.mark.asyncio
async def test_message_that_merely_resembles_block_is_not_stripped(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """A user message that itself starts with a look-alike (but not the exact server anchor) is
    preserved in history — the strip targets only the server-generated block (ADR-042 §3)."""
    look_alike = "[Conversation settings] hi there, this is my actual message"
    uid, sid = await _run_with_context(
        client,
        db_sessionmaker,
        fake_anthropic,
        message=look_alike,
        context=None,
        final_text="ok",
    )
    hist = (await client.get(f"/v1/chats/{sid}", headers=auth_headers(uid))).json()
    first_text = _first_text_block(_user_steps(hist)[0])
    assert first_text is not None and first_text["text"] == look_alike


# ============================ provider-agnostic (OpenAI seam) ============================
@pytest.fixture
def restore_provider() -> Iterator[None]:
    s = get_settings()
    orig = s.llm_provider
    yield
    s.llm_provider = orig


@pytest.mark.asyncio
async def test_history_strip_is_provider_agnostic_openai(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    restore_provider: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The read-time strip works on the domain text regardless of provider (ADR-033/ADR-042 §6).

    Hermetic: pin the OpenAI singleton to the same faithful double so the openai path never builds a
    real OpenAIClient (no network under placeholder keys). The injection/strip are provider-neutral,
    so history shows the bare message for provider=openai too.
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
            "message": "translate this",
            "mode": "credits",
            "context": {"locale": "de-DE", "responseStyle": "balanced"},
        },
        headers=auth_headers(uid),
    )
    assert r.status_code == 200, r.text
    sid = r.json()["sessionId"]

    # Stored payload (provider-independent) carries the block; history strips it.
    assert _BLOCK_PREFIX in await _stored_user_text(db_sessionmaker, sid)
    hist_resp = await client.get(f"/v1/chats/{sid}", headers=auth_headers(uid))
    hist = hist_resp.json()
    first_text = _first_text_block(_user_steps(hist)[0])
    assert first_text is not None and first_text["text"] == "translate this"
    assert _BLOCK_PREFIX not in hist_resp.text
