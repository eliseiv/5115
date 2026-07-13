"""Integration tests for inline base64 attachments through /v1/chat/run (ADR-020).

Real PostgreSQL container, Anthropic faked at the client boundary. Covers the integration-level
ADR-020 cases (06-testing-strategy.md): per-route body limit (413 only on /v1/chat/run, other
routes keep ≤512KB), the replay/storage invariant (chat_steps.payload holds placeholders not
base64; on tool-loop turn >=1 the heavy block is NOT replayed to Anthropic), API-level attachment
rejections (422), billing unchanged (1 credit), redaction in audit, and /chat/tool-result not
accepting attachments.
"""

from __future__ import annotations

import base64
import io
import json
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import FakeAnthropicClient, auth_headers, seed_user
from tests.fake_client_tool import FAKE_CLIENT_TOOL, register_fake_client_tool


@pytest.fixture(autouse=True)
def _register_fake_client_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    # ADR-063: register a test-only client-side example tool (no shipped client-side tool remains).
    register_fake_client_tool(monkeypatch)


_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _pdf_b64(pages: int = 1) -> str:
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=72, height=72)
    buf = io.BytesIO()
    writer.write(buf)
    return _b64(buf.getvalue())


def _png_attachment() -> dict[str, str]:
    return {"type": "image", "mediaType": "image/png", "filename": "p.png", "data": _b64(_PNG)}


# --- scenario 10: billing unchanged (1 credit) ---
@pytest.mark.asyncio
async def test_message_with_attachment_debits_one_credit(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [fake_anthropic.text_result("seen the photo")]

    r = await client.post(
        "/v1/chat/run",
        json={
            "userId": str(uid),
            "projectId": "p",
            "message": "what is this?",
            "mode": "credits",
            "attachments": [_png_attachment()],
        },
        headers=auth_headers(uid),
    )
    assert r.status_code == 200
    assert r.json()["status"] == "assistant_message"

    async with db_sessionmaker() as s:
        bal = await s.scalar(text("SELECT balance FROM wallets WHERE user_id=:u"), {"u": str(uid)})
        debits = await s.scalar(
            text("SELECT count(*) FROM ledger_transactions WHERE user_id=:u AND type='debit'"),
            {"u": str(uid)},
        )
    assert int(bal) == 4  # exactly one credit consumed
    assert int(debits) == 1


@pytest.mark.asyncio
async def test_message_with_attachment_byok_no_debit(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(
            s, subscription="active", balance=0, byok_enabled=True, byok_status="valid"
        )
    fake_anthropic.responses = [fake_anthropic.text_result("ok")]
    r = await client.post(
        "/v1/chat/run",
        json={
            "userId": str(uid),
            "projectId": "p",
            "message": "hi",
            "mode": "byok",
            "attachments": [_png_attachment()],
        },
        headers=auth_headers(uid),
    )
    assert r.json()["status"] == "assistant_message"
    async with db_sessionmaker() as s:
        debits = await s.scalar(
            text("SELECT count(*) FROM ledger_transactions WHERE user_id=:u AND type='debit'"),
            {"u": str(uid)},
        )
    assert int(debits) == 0


# --- scenario 1 (integration): full block sent to Anthropic on turn 0 ---
@pytest.mark.asyncio
async def test_attachment_full_block_sent_to_anthropic_on_first_turn(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [fake_anthropic.text_result("done")]

    await client.post(
        "/v1/chat/run",
        json={
            "userId": str(uid),
            "projectId": "p",
            "message": "describe",
            "mode": "credits",
            "attachments": [_png_attachment()],
        },
        headers=auth_headers(uid),
    )
    # The FIRST (only) Anthropic call carries the full image block with the original base64.
    first_call_messages = fake_anthropic.calls[0]["messages"]
    image_blocks = [
        b
        for m in first_call_messages
        if isinstance(m.get("content"), list)
        for b in m["content"]
        if isinstance(b, dict) and b.get("type") == "image"
    ]
    assert len(image_blocks) == 1
    assert image_blocks[0]["source"]["data"] == _b64(_PNG)


# --- scenario 7: storage invariant + no heavy replay on turn >=1 ---
@pytest.mark.asyncio
async def test_storage_invariant_and_no_heavy_replay_in_tool_loop(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)

    # run (with attachment) -> tool_call ; tool-result -> assistant_message.
    fake_anthropic.responses = [
        fake_anthropic.tool_result(FAKE_CLIENT_TOOL, {"path": "a.txt"}),
        fake_anthropic.text_result("final"),
    ]
    data_b64 = _b64(_PNG)

    r1 = await client.post(
        "/v1/chat/run",
        json={
            "userId": str(uid),
            "projectId": "p",
            "message": "analyze",
            "mode": "credits",
            "attachments": [_png_attachment()],
        },
        headers=auth_headers(uid),
    )
    b1 = r1.json()
    assert b1["status"] == "tool_call"
    sess = b1["sessionId"]
    tcid = b1["toolCall"]["id"]

    # --- storage invariant: the persisted user step holds a placeholder, NEVER the raw base64 ---
    async with db_sessionmaker() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT payload FROM chat_steps WHERE session_id=:sid AND role='user' "
                    "ORDER BY created_at"
                ),
                {"sid": sess},
            )
        ).all()
    assert rows, "user step must be persisted"
    user_payload = rows[0][0]
    serialized = json.dumps(user_payload)
    assert data_b64 not in serialized  # raw base64 never stored
    # A light text placeholder mentioning the attachment is present instead.
    placeholder_texts = [
        blk["text"]
        for blk in user_payload["content"]
        if blk.get("type") == "text" and "attachment" in blk.get("text", "")
    ]
    assert placeholder_texts and "image/png" in placeholder_texts[0]

    # Continue the tool-loop.
    r2 = await client.post(
        "/v1/chat/tool-result",
        json={"userId": str(uid), "sessionId": sess, "toolCallId": tcid, "result": {"ok": 1}},
        headers=auth_headers(uid),
    )
    assert r2.json()["status"] == "assistant_message"

    # no heavy replay: the continuation call (turn >=1) must NOT contain the full image block.
    continuation_messages = fake_anthropic.calls[-1]["messages"]
    image_blocks = [
        b
        for m in continuation_messages
        if isinstance(m.get("content"), list)
        for b in m["content"]
        if isinstance(b, dict) and b.get("type") == "image"
    ]
    assert image_blocks == []  # heavy base64 not re-sent on tool-continuation
    assert data_b64 not in json.dumps(continuation_messages)
    # But the placeholder text is still there so the model retains the attachment context.
    assert "attachment" in json.dumps(continuation_messages)


# --- scenario 8: redaction in audit (no base64 / decoded content) ---
@pytest.mark.asyncio
async def test_audit_logs_contain_no_attachment_data(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [fake_anthropic.text_result("ok")]
    data_b64 = _b64(_PNG)

    await client.post(
        "/v1/chat/run",
        json={
            "userId": str(uid),
            "projectId": "p",
            "message": "hi",
            "mode": "credits",
            "attachments": [_png_attachment()],
        },
        headers=auth_headers(uid),
    )
    async with db_sessionmaker() as s:
        rows = (
            await s.execute(
                text("SELECT payload FROM audit_logs WHERE user_id=:u"), {"u": str(uid)}
            )
        ).all()
    assert rows
    blob = json.dumps([r[0] for r in rows])
    assert data_b64 not in blob  # raw base64 never reaches audit


# ----------------------------- scenario 6: per-route body limit -----------------------------
@pytest.mark.asyncio
async def test_large_body_allowed_on_chat_run_route(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    # A body well above the general 512KB limit but below the 12MB chat-run limit must NOT be
    # rejected by the size middleware (it reaches the handler -> 200). ~700KB content-length.
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [fake_anthropic.text_result("ok")]
    big_png = _b64(_PNG + b"\x00" * (700 * 1024))  # ~700KB decoded -> well within 5MB image limit

    r = await client.post(
        "/v1/chat/run",
        json={
            "userId": str(uid),
            "projectId": "p",
            "message": "big",
            "mode": "credits",
            "attachments": [{"type": "image", "mediaType": "image/png", "data": big_png}],
        },
        headers=auth_headers(uid),
    )
    # Passed the size middleware (not 413) and was accepted (valid PNG magic) -> 200.
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_large_body_rejected_on_other_route_413(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # The same oversized body on a different route keeps the general ≤512KB limit -> 413.
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    headers = {**auth_headers(uid), "content-length": str(600 * 1024)}
    r = await client.post(
        "/v1/policy/effective",
        content=b"x" * (600 * 1024),
        headers=headers,
    )
    assert r.status_code == 413


# --- scenario 2/3/5 (integration): API rejections are 422 not 500 ---
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "attachment",
    [
        # magic-byte spoof: declared png, body is not png.
        {"type": "image", "mediaType": "image/png", "data": _b64(b"\xff\xd8\xff not a png")},
        # invalid base64.
        {"type": "image", "mediaType": "image/png", "data": "@@@not-base64@@@"},
        # invalid UTF-8 text.
        {"type": "text", "mediaType": "text/plain", "data": _b64(b"\xff\xfe\xfa")},
        # corrupt PDF (valid magic, garbage body).
        {"type": "document", "mediaType": "application/pdf", "data": _b64(b"%PDF-1.4 garbage")},
    ],
)
async def test_bad_attachment_returns_422(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    attachment: dict[str, str],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    r = await client.post(
        "/v1/chat/run",
        json={
            "userId": str(uid),
            "projectId": "p",
            "message": "hi",
            "mode": "credits",
            "attachments": [attachment],
        },
        headers=auth_headers(uid),
    )
    assert r.status_code == 422, r.text
    # No Anthropic call and no debit on a rejected attachment.
    assert not fake_anthropic.calls
    async with db_sessionmaker() as s:
        debits = await s.scalar(
            text("SELECT count(*) FROM ledger_transactions WHERE user_id=:u AND type='debit'"),
            {"u": str(uid)},
        )
    assert int(debits) == 0


@pytest.mark.asyncio
async def test_mime_outside_allowlist_returns_422(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    r = await client.post(
        "/v1/chat/run",
        json={
            "userId": str(uid),
            "projectId": "p",
            "message": "hi",
            "mode": "credits",
            "attachments": [{"type": "image", "mediaType": "application/zip", "data": "AAAA"}],
        },
        headers=auth_headers(uid),
    )
    assert r.status_code == 422


# --- scenario 9 (API): tool-result rejects attachments ---
@pytest.mark.asyncio
async def test_tool_result_rejects_attachments_422(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    r = await client.post(
        "/v1/chat/tool-result",
        json={
            "userId": str(uid),
            "sessionId": str(uuid.uuid4()),
            "toolCallId": str(uuid.uuid4()),
            "result": {"ok": True},
            "attachments": [{"type": "image", "mediaType": "image/png", "data": "AAAA"}],
        },
        headers=auth_headers(uid),
    )
    assert r.status_code == 422  # extra='forbid' on ChatToolResultRequest
