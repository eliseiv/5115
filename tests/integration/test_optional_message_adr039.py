"""Tests for ADR-039 — optional `message` in /v1/chat/run when attachments are present.

Real PostgreSQL container; the LLM client is faked at the create_message boundary (conftest
`FakeAnthropicClient`), which records the WIRE view (exactly what the provider would receive) AND
the persisted user-step payload. ADR-039 turn-0 assembly happens in `orchestrator.run()` BEFORE the
provider client, so we assert on (a) the schema validator (§1: «message OR ≥1 attachment»), (b) the
`_compose_turn0_text` helper matrix (§3 context-block splice), (c) the actual user content that
reached create_message — no blank `{"type":"text","text":""}` block on an image-/file-only turn —
and (d) provider-agnosticism (the empty text block is sent to NEITHER provider).

Covers (follow_up_for_qa):
1. 422: empty message + no attachments → «message or at least one attachment is required»;
2. 422: whitespace-only message + no attachments;
3. 422: empty message + only workspaceProjectId (no request attachments);
4. 200 image-only: empty message + 1 image → user turn has NO empty text block (only the image
   content block + its persisted placeholder);
5. 200 file-only: empty message + 1 text/document attachment → assembled with no empty text block;
6. splice: empty message + context block → text block == block WITHOUT a trailing "\n\n";
7. splice: whitespace-only message + attachment → no text block created;
8. regression: non-empty message (with/without attachments) → unchanged (text block leads, then
   placeholders);
9. 422 preserved: non-empty message > size_limit_message;
10. provider-agnostic: image-only identical on anthropic and openai (no empty text block to either).
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.chat.orchestrator import _compose_turn0_text
from app.config import get_settings
from tests.conftest import FakeAnthropicClient, auth_headers, seed_user

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
_BLOCK_PREFIX = "[Conversation settings for this message:"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _png_attachment() -> dict[str, str]:
    return {"type": "image", "mediaType": "image/png", "filename": "p.png", "data": _b64(_PNG)}


def _user_wire_content(fake: FakeAnthropicClient, call_index: int = 0) -> list[dict[str, Any]]:
    """The wire content list of the last user message of the given create_message call."""
    msgs = fake.calls[call_index]["messages"]
    user_msgs = [m for m in msgs if m.get("role") == "user"]
    content = user_msgs[-1]["content"]
    assert isinstance(content, list), content
    return [b for b in content if isinstance(b, dict)]


async def _persisted_user_content(
    maker: async_sessionmaker[AsyncSession], session_id: str
) -> list[dict[str, Any]]:
    """The persisted turn-0 user-step content blocks (first user step by seq)."""
    async with maker() as s:
        payload = await s.scalar(
            text(
                "SELECT payload FROM chat_steps WHERE session_id=:sid AND role='user' "
                "ORDER BY seq LIMIT 1"
            ),
            {"sid": session_id},
        )
    assert payload is not None
    return list(payload["content"])


def _empty_text_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Blank `{"type":"text","text":""}` (or whitespace-only) user-message blocks — must be absent.

    Excludes ADR-020 attachment placeholders / inlined text-file blocks: a real empty user-message
    text block has `text` empty after strip; placeholders always start with "[attachment:" and
    text-file blocks carry the filename + fenced content (never empty).
    """
    return [b for b in blocks if b.get("type") == "text" and not str(b.get("text", "")).strip()]


# ============================ §1: schema validator (422) ============================
@pytest.mark.asyncio
async def test_empty_message_no_attachments_is_422(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """Scenario 1: empty message + no attachments → 422 with the ADR-039 message; no upstream."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "", "mode": "credits"},
        headers=auth_headers(uid),
    )
    # The validator raises ValueError("message or at least one attachment is required") → 422; the
    # API redacts the detail into a generic envelope, so we assert on the 422 status + no upstream
    # (the specific ValueError text is verified at the schema layer in the unit test below).
    assert r.status_code == 422, r.text
    assert not fake_anthropic.calls  # validation runs before any upstream call


@pytest.mark.asyncio
async def test_missing_message_field_no_attachments_is_422(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """Scenario 1 variant: omitting `message` entirely (default "") + no attachments → 422."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert r.status_code == 422, r.text
    assert not fake_anthropic.calls


@pytest.mark.asyncio
async def test_whitespace_only_message_no_attachments_is_422(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """Scenario 2: whitespace-only message ("   ") + no attachments → 422 (validator uses strip)."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "   \n\t ", "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert r.status_code == 422, r.text
    assert not fake_anthropic.calls


@pytest.mark.asyncio
async def test_empty_message_with_only_workspace_project_id_is_422(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """Scenario 3 (ADR-039 §6 edge): empty message + only workspaceProjectId, no request
    attachments → 422. The «≥1 attachment» rule is about REQUEST attachments; a workspace binding
    does not satisfy it (the validator rejects pre-orchestration, before any workspace lookup)."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    r = await client.post(
        "/v1/chat/run",
        json={
            "userId": str(uid),
            "message": "",
            "mode": "credits",
            "workspaceProjectId": str(uuid.uuid4()),
        },
        headers=auth_headers(uid),
    )
    assert r.status_code == 422, r.text
    assert not fake_anthropic.calls


@pytest.mark.asyncio
async def test_empty_attachments_list_is_422(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """ADR-039 §6: empty message + empty `attachments: []` → 422 (no content carried)."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "", "mode": "credits", "attachments": []},
        headers=auth_headers(uid),
    )
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_non_empty_message_over_size_limit_is_422(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """Scenario 9: the message size limit is preserved (a non-empty message > limit → 422)."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    big = "x" * (get_settings().size_limit_message + 100)
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": big, "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert r.status_code == 422, r.text
    assert not fake_anthropic.calls


# ============================ §3: _compose_turn0_text helper matrix ============================
@pytest.mark.parametrize(
    ("block", "msg", "expected"),
    [
        # non-empty message — unchanged behavior.
        ("[B]", "hi", "[B]\n\nhi"),
        (None, "hi", "hi"),
        # empty message + block → bare block, NO trailing "\n\n".
        ("[B]", "", "[B]"),
        ("[B]", "   ", "[B]"),  # whitespace-only treated as «no text»
        # empty message + no block → "" → caller omits the text block.
        (None, "", ""),
        (None, "   \t ", ""),  # whitespace-only + no block → ""
    ],
)
def test_compose_turn0_text_matrix(block: str | None, msg: str, expected: str) -> None:
    """ADR-039 §3 exact splice matrix; whitespace-only message is «no text» (symmetric with §1)."""
    assert _compose_turn0_text(block, msg) == expected


# ===================== §1: schema-level validator message (exact text) =====================
@pytest.mark.parametrize("message", ["", "   ", "\n\t "])
def test_schema_rejects_empty_message_without_attachments(message: str) -> None:
    """At the schema layer the validator raises the exact ADR-039 §1 ValueError. The HTTP layer
    redacts this into a generic envelope (asserted as 422 in the integration tests above), so the
    precise message is verified here against the model directly."""
    import pydantic

    from app.schemas.chat import ChatRunRequest

    with pytest.raises(pydantic.ValidationError, match="message or at least one attachment"):
        ChatRunRequest.model_validate(
            {"userId": str(uuid.uuid4()), "message": message, "mode": "credits"}
        )


def test_schema_accepts_empty_message_with_attachment() -> None:
    """An empty message is valid when ≥1 request attachment is present (§1)."""
    from app.schemas.chat import ChatRunRequest

    req = ChatRunRequest.model_validate(
        {
            "userId": str(uuid.uuid4()),
            "message": "",
            "mode": "credits",
            "attachments": [_png_attachment()],
        }
    )
    assert req.message == ""
    assert req.attachments is not None and len(req.attachments) == 1


def test_schema_message_size_limit_preserved() -> None:
    """The message size limit raises the exact ValueError at the schema layer (§1, scenario 9)."""
    import pydantic

    from app.schemas.chat import ChatRunRequest

    big = "x" * (get_settings().size_limit_message + 100)
    with pytest.raises(pydantic.ValidationError, match="message exceeds size limit"):
        ChatRunRequest.model_validate(
            {"userId": str(uuid.uuid4()), "message": big, "mode": "credits"}
        )


# ============================ §2/§4: image-only / file-only turn ============================
@pytest.mark.asyncio
async def test_image_only_no_empty_text_block(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """Scenario 4: empty message + 1 image → user turn carries the image block but NO blank text
    block (`{"type":"text","text":""}`); persisted payload holds only the attachment placeholder."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [fake_anthropic.text_result("seen")]
    r = await client.post(
        "/v1/chat/run",
        json={
            "userId": str(uid),
            "message": "",
            "mode": "credits",
            "attachments": [_png_attachment()],
        },
        headers=auth_headers(uid),
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "assistant_message"

    # Wire: exactly one image block, NO blank text block reaches the provider.
    wire = _user_wire_content(fake_anthropic)
    assert _empty_text_blocks(wire) == []
    image_blocks = [b for b in wire if b.get("type") == "image"]
    assert len(image_blocks) == 1
    assert image_blocks[0]["source"]["data"] == _b64(_PNG)

    # Persisted: only the ADR-020 placeholder, no empty user text block.
    persisted = await _persisted_user_content(db_sessionmaker, r.json()["sessionId"])
    assert _empty_text_blocks(persisted) == []
    text_blocks = [b for b in persisted if b.get("type") == "text"]
    assert len(text_blocks) == 1
    assert text_blocks[0]["text"].startswith("[attachment:")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "attachment",
    [
        {"type": "text", "mediaType": "text/plain", "filename": "n.txt", "data": _b64(b"body")},
    ],
)
async def test_file_only_no_empty_text_block(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    attachment: dict[str, str],
) -> None:
    """Scenario 5: empty message + 1 text-file attachment → assembled with no blank user text
    block. The inlined text-file block (filename + fenced content) is NOT an empty block; no
    separate empty user-message text block is created."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [fake_anthropic.text_result("parsed")]
    r = await client.post(
        "/v1/chat/run",
        json={
            "userId": str(uid),
            "message": "",
            "mode": "credits",
            "attachments": [attachment],
        },
        headers=auth_headers(uid),
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "assistant_message"

    wire = _user_wire_content(fake_anthropic)
    assert _empty_text_blocks(wire) == []
    # The text-file content block is present and non-empty (filename + fenced body).
    file_blocks = [b for b in wire if b.get("type") == "text" and "```" in str(b.get("text", ""))]
    assert len(file_blocks) == 1
    assert "body" in file_blocks[0]["text"]

    persisted = await _persisted_user_content(db_sessionmaker, r.json()["sessionId"])
    assert _empty_text_blocks(persisted) == []


@pytest.mark.asyncio
async def test_document_only_no_empty_text_block(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """Scenario 5 (document variant): empty message + 1 PDF document → no blank user text block;
    the native document block reaches the provider, only the placeholder is persisted."""
    import io

    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    buf = io.BytesIO()
    writer.write(buf)
    pdf_b64 = _b64(buf.getvalue())

    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [fake_anthropic.text_result("read pdf")]
    r = await client.post(
        "/v1/chat/run",
        json={
            "userId": str(uid),
            "message": "",
            "mode": "credits",
            "attachments": [
                {
                    "type": "document",
                    "mediaType": "application/pdf",
                    "filename": "d.pdf",
                    "data": pdf_b64,
                }
            ],
        },
        headers=auth_headers(uid),
    )
    assert r.status_code == 200, r.text
    wire = _user_wire_content(fake_anthropic)
    assert _empty_text_blocks(wire) == []
    assert any(b.get("type") == "document" for b in wire)

    persisted = await _persisted_user_content(db_sessionmaker, r.json()["sessionId"])
    assert _empty_text_blocks(persisted) == []
    text_blocks = [b for b in persisted if b.get("type") == "text"]
    assert len(text_blocks) == 1
    assert text_blocks[0]["text"].startswith("[attachment:")


# ===================== §3: context-block splice with empty message =====================
@pytest.mark.asyncio
async def test_empty_message_with_context_block_no_trailing_newlines(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """Scenario 6: empty message + context (codeLanguage) + attachment → the leading text block is
    the context block alone, WITHOUT a dangling "\\n\\n" and without a separate empty block."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [fake_anthropic.text_result("ok")]
    r = await client.post(
        "/v1/chat/run",
        json={
            "userId": str(uid),
            "message": "",
            "mode": "credits",
            "attachments": [_png_attachment()],
            "context": {"codeLanguage": "Swift"},
        },
        headers=auth_headers(uid),
    )
    assert r.status_code == 200, r.text

    persisted = await _persisted_user_content(db_sessionmaker, r.json()["sessionId"])
    # The leading text block must be the bare context block — no trailing "\n\n", no empty block.
    block_texts = [
        b["text"]
        for b in persisted
        if b.get("type") == "text" and str(b["text"]).startswith(_BLOCK_PREFIX)
    ]
    assert len(block_texts) == 1
    block_text = block_texts[0]
    assert block_text == "[Conversation settings for this message: codeLanguage=Swift]"
    assert not block_text.endswith("\n")
    assert _empty_text_blocks(persisted) == []


@pytest.mark.asyncio
async def test_whitespace_only_message_with_attachment_no_text_block(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """Scenario 7: whitespace-only message + attachment (no context) → valid (§1: has attachment),
    and NO whitespace-only text block is created (§3 uses `not msg.strip()`)."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [fake_anthropic.text_result("ok")]
    r = await client.post(
        "/v1/chat/run",
        json={
            "userId": str(uid),
            "message": "   \n  ",
            "mode": "credits",
            "attachments": [_png_attachment()],
        },
        headers=auth_headers(uid),
    )
    assert r.status_code == 200, r.text

    wire = _user_wire_content(fake_anthropic)
    assert _empty_text_blocks(wire) == []
    persisted = await _persisted_user_content(db_sessionmaker, r.json()["sessionId"])
    assert _empty_text_blocks(persisted) == []
    # Only the attachment placeholder text block, nothing else.
    text_blocks = [b for b in persisted if b.get("type") == "text"]
    assert len(text_blocks) == 1
    assert text_blocks[0]["text"].startswith("[attachment:")


# ===================== §7: regression — non-empty message unchanged =====================
@pytest.mark.asyncio
async def test_non_empty_message_no_attachments_unchanged(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """Scenario 8: non-empty message, no attachments → single text block == the bare message."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [fake_anthropic.text_result("ok")]
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "hello world", "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert r.status_code == 200, r.text

    persisted = await _persisted_user_content(db_sessionmaker, r.json()["sessionId"])
    assert persisted == [{"type": "text", "text": "hello world"}]


@pytest.mark.asyncio
async def test_non_empty_message_with_attachment_text_block_leads(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """Scenario 8: non-empty message + attachment → text block FIRST, then attachment placeholder
    (order unchanged); on the wire the text block leads, then the image content block."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [fake_anthropic.text_result("ok")]
    r = await client.post(
        "/v1/chat/run",
        json={
            "userId": str(uid),
            "message": "what is this?",
            "mode": "credits",
            "attachments": [_png_attachment()],
        },
        headers=auth_headers(uid),
    )
    assert r.status_code == 200, r.text

    persisted = await _persisted_user_content(db_sessionmaker, r.json()["sessionId"])
    assert persisted[0] == {"type": "text", "text": "what is this?"}
    assert persisted[1]["type"] == "text"
    assert persisted[1]["text"].startswith("[attachment:")

    # Wire: text block leads, image content block follows.
    wire = _user_wire_content(fake_anthropic)
    assert wire[0] == {"type": "text", "text": "what is this?"}
    assert any(b.get("type") == "image" for b in wire)


# ============================ §4: provider-agnostic image-only ============================
@pytest.fixture
def restore_provider() -> Iterator[None]:
    s = get_settings()
    orig = s.llm_provider
    yield
    s.llm_provider = orig


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["anthropic", "openai"])
async def test_image_only_provider_agnostic_no_empty_text_block(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    restore_provider: None,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    """Scenario 10: image-only behaves identically on anthropic and openai — the empty text block is
    sent to NEITHER provider. The decision lives in `orchestrator.run()` before the provider client,
    so the persisted (provider-independent) user content is the single source serialized per
    provider.

    Hermetic OpenAI path: with provider=openai the `get_llm_client` factory would build a REAL
    `OpenAIClient` (network on create_message → 401 under a placeholder key in CI). Mirror the
    conftest anthropic-singleton patch on the OpenAI seam: pin `llm_client._openai_singleton` to the
    faithful `LLMClient` double so the factory returns the fake on the openai path — no
    `OpenAIClient()` construction, no network. `monkeypatch` auto-restores the singleton.
    """
    from app.chat import llm_client as llm_client_mod

    get_settings().llm_provider = provider
    if provider == "openai":
        monkeypatch.setattr(llm_client_mod, "_openai_singleton", fake_anthropic, raising=False)

    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [fake_anthropic.text_result("ok")]
    r = await client.post(
        "/v1/chat/run",
        json={
            "userId": str(uid),
            "message": "",
            "mode": "credits",
            "attachments": [_png_attachment()],
        },
        headers=auth_headers(uid),
    )
    assert r.status_code == 200, r.text

    # Provider-independent persisted user content: only the placeholder, no empty text block.
    persisted = await _persisted_user_content(db_sessionmaker, r.json()["sessionId"])
    assert _empty_text_blocks(persisted) == []
    text_blocks = [b for b in persisted if b.get("type") == "text"]
    assert len(text_blocks) == 1
    assert text_blocks[0]["text"].startswith("[attachment:")
