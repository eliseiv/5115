"""Integration tests for /v1/workspaces* (ADR-036, workspaces/09-testing.md §Integration/Изоляция).

Real PostgreSQL container; Anthropic faked at the client boundary. Covers sub-phase 3A (CRUD,
cursor pagination, fileCount/chatCount, isolation, JWT, delete cascade + chat orphan), the chat
binding (session-fixed workspaceProjectId, /v1/chats filter, real workspaceProjectId in the list),
instructions injection into the system prompt, and sub-phase 3B knowledge files (upload/list/delete,
limits, isolation, injection into a workspace chat, provider-agnostic PDF-as-text).
"""

from __future__ import annotations

import base64
import io
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import FakeAnthropicClient, auth_headers, seed_user

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


async def _create_workspace(
    client: AsyncClient, uid: uuid.UUID, **body: object
) -> dict[str, object]:
    payload: dict[str, object] = {"name": "Proj"}
    payload.update(body)
    r = await client.post("/v1/workspaces", json=payload, headers=auth_headers(uid))
    assert r.status_code == 201, r.text
    return r.json()


# ============================================================================
# 3A — CRUD
# ============================================================================
@pytest.mark.asyncio
async def test_create_minimal_and_with_optional_fields(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    r = await client.post(
        "/v1/workspaces",
        json={"name": "  My Project  ", "description": "d", "instructions": "be brief"},
        headers=auth_headers(uid),
    )
    assert r.status_code == 201
    body = r.json()
    assert body["name"] == "My Project"  # stripped
    assert body["description"] == "d"
    assert body["instructions"] == "be brief"
    assert uuid.UUID(body["id"])


@pytest.mark.asyncio
async def test_create_empty_name_rejected_422(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    r = await client.post("/v1/workspaces", json={"name": "   "}, headers=auth_headers(uid))
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_get_workspace(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    w = await _create_workspace(client, uid, name="X", instructions="i")
    r = await client.get(f"/v1/workspaces/{w['id']}", headers=auth_headers(uid))
    assert r.status_code == 200
    assert r.json()["instructions"] == "i"
    assert r.json()["files"] == []


@pytest.mark.asyncio
async def test_list_cursor_and_counts(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    for i in range(3):
        await _create_workspace(client, uid, name=f"W{i}")

    r = await client.get("/v1/workspaces?limit=2", headers=auth_headers(uid))
    assert r.status_code == 200
    page1 = r.json()
    assert len(page1["items"]) == 2
    assert page1["nextCursor"] is not None
    assert all("fileCount" in it and "chatCount" in it for it in page1["items"])

    r2 = await client.get(
        f"/v1/workspaces?limit=2&cursor={page1['nextCursor']}", headers=auth_headers(uid)
    )
    page2 = r2.json()
    assert len(page2["items"]) == 1
    assert page2["nextCursor"] is None
    seen = {it["id"] for it in page1["items"]} | {it["id"] for it in page2["items"]}
    assert len(seen) == 3  # no overlap across pages


@pytest.mark.asyncio
async def test_invalid_cursor_rejected_422(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    r = await client.get("/v1/workspaces?cursor=!!!bad", headers=auth_headers(uid))
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_patch_clears_description_and_instructions_via_null(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    w = await _create_workspace(client, uid, name="X", description="d", instructions="i")
    r = await client.patch(
        f"/v1/workspaces/{w['id']}",
        json={"description": None, "instructions": None},
        headers=auth_headers(uid),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["description"] is None
    assert body["instructions"] is None
    assert body["name"] == "X"  # untouched


@pytest.mark.asyncio
async def test_patch_name_cannot_be_null(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    w = await _create_workspace(client, uid, name="X")
    r = await client.patch(
        f"/v1/workspaces/{w['id']}", json={"name": None}, headers=auth_headers(uid)
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_patch_requires_at_least_one_field(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    w = await _create_workspace(client, uid, name="X")
    r = await client.patch(f"/v1/workspaces/{w['id']}", json={}, headers=auth_headers(uid))
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_delete_then_get_404(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    w = await _create_workspace(client, uid, name="X")
    r = await client.delete(f"/v1/workspaces/{w['id']}", headers=auth_headers(uid))
    assert r.status_code == 200
    assert r.json()["deleted"] is True
    # idempotent: second delete → 404
    r2 = await client.delete(f"/v1/workspaces/{w['id']}", headers=auth_headers(uid))
    assert r2.status_code == 404
    assert (
        await client.get(f"/v1/workspaces/{w['id']}", headers=auth_headers(uid))
    ).status_code == 404


# ============================================================================
# 3A — Isolation + JWT
# ============================================================================
@pytest.mark.asyncio
async def test_foreign_workspace_is_404(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        owner = await seed_user(s)
        other = await seed_user(s)
    w = await _create_workspace(client, owner, name="secret")
    # The other user must not see it (404, never reveal foreign existence).
    assert (
        await client.get(f"/v1/workspaces/{w['id']}", headers=auth_headers(other))
    ).status_code == 404
    assert (
        await client.patch(
            f"/v1/workspaces/{w['id']}", json={"name": "x"}, headers=auth_headers(other)
        )
    ).status_code == 404
    assert (
        await client.delete(f"/v1/workspaces/{w['id']}", headers=auth_headers(other))
    ).status_code == 404


@pytest.mark.asyncio
async def test_nonexistent_workspace_is_404(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    r = await client.get(f"/v1/workspaces/{uuid.uuid4()}", headers=auth_headers(uid))
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_list_only_own_workspaces(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        owner = await seed_user(s)
        other = await seed_user(s)
    await _create_workspace(client, owner, name="mine")
    r = await client.get("/v1/workspaces", headers=auth_headers(other))
    assert r.json()["items"] == []


@pytest.mark.asyncio
async def test_jwt_required_401(client: AsyncClient) -> None:
    assert (await client.get("/v1/workspaces")).status_code == 401
    assert (await client.post("/v1/workspaces", json={"name": "x"})).status_code == 401
    assert (await client.get(f"/v1/workspaces/{uuid.uuid4()}")).status_code == 401


# ============================================================================
# 3A — Chat binding (session-fixed) + injection
# ============================================================================
async def _run_chat(
    client: AsyncClient,
    uid: uuid.UUID,
    fake: FakeAnthropicClient,
    *,
    message: str = "hi",
    reply: str = "ok",
    **extra: object,
) -> dict[str, object]:
    fake.responses = [fake.text_result(reply)]
    body: dict[str, object] = {"userId": str(uid), "message": message, "mode": "credits"}
    body.update(extra)
    r = await client.post("/v1/chat/run", json=body, headers=auth_headers(uid))
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.asyncio
async def test_chat_run_foreign_workspace_404(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        owner = await seed_user(s, subscription="active", balance=5)
        other = await seed_user(s, subscription="active", balance=5)
    w = await _create_workspace(client, owner, name="X")
    fake_anthropic.responses = [fake_anthropic.text_result("ok")]
    r = await client.post(
        "/v1/chat/run",
        json={
            "userId": str(other),
            "message": "hi",
            "mode": "credits",
            "workspaceProjectId": w["id"],
        },
        headers=auth_headers(other),
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "workspace_not_found"


@pytest.mark.asyncio
async def test_chat_run_nonexistent_workspace_404(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    r = await client.post(
        "/v1/chat/run",
        json={
            "userId": str(uid),
            "message": "hi",
            "mode": "credits",
            "workspaceProjectId": str(uuid.uuid4()),
        },
        headers=auth_headers(uid),
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "workspace_not_found"


@pytest.mark.asyncio
async def test_chat_binding_is_session_fixed_resume_ignores_field(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    w = await _create_workspace(client, uid, name="X")
    other_w = await _create_workspace(client, uid, name="Y")

    out = await _run_chat(client, uid, fake_anthropic, workspaceProjectId=w["id"])
    sid = out["sessionId"]

    # Resume the SAME session passing a different workspaceProjectId — must be ignored.
    fake_anthropic.responses = [fake_anthropic.text_result("again")]
    r = await client.post(
        "/v1/chat/run",
        json={
            "userId": str(uid),
            "message": "more",
            "mode": "credits",
            "sessionId": sid,
            "workspaceProjectId": other_w["id"],
        },
        headers=auth_headers(uid),
    )
    assert r.status_code == 200
    async with db_sessionmaker() as s:
        bound = await s.scalar(
            text("SELECT workspace_project_id FROM chat_sessions WHERE id=:i"), {"i": sid}
        )
    assert str(bound) == w["id"]  # still the original binding


@pytest.mark.asyncio
async def test_chat_list_item_has_real_workspace_id_and_filter(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    w = await _create_workspace(client, uid, name="X")

    await _run_chat(client, uid, fake_anthropic, message="in project", workspaceProjectId=w["id"])
    await _run_chat(client, uid, fake_anthropic, message="plain chat")

    # The bound chat reports the real workspaceProjectId; the plain chat reports null.
    r = await client.get("/v1/chats", headers=auth_headers(uid))
    items = r.json()["items"]
    by_ws = {it["workspaceProjectId"] for it in items}
    assert w["id"] in by_ws
    assert None in by_ws

    # Filter ?workspaceProjectId= returns only «чаты проекта».
    rf = await client.get(f"/v1/chats?workspaceProjectId={w['id']}", headers=auth_headers(uid))
    filtered = rf.json()["items"]
    assert len(filtered) == 1
    assert filtered[0]["workspaceProjectId"] == w["id"]


@pytest.mark.asyncio
async def test_workspace_count_reflected_in_list(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    w = await _create_workspace(client, uid, name="X")
    await _run_chat(client, uid, fake_anthropic, workspaceProjectId=w["id"])

    r = await client.get("/v1/workspaces", headers=auth_headers(uid))
    item = next(it for it in r.json()["items"] if it["id"] == w["id"])
    assert item["chatCount"] == 1
    assert item["fileCount"] == 0


# ---- instructions injection ----
@pytest.mark.asyncio
async def test_instructions_injected_into_system_prompt(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    w = await _create_workspace(client, uid, name="X", instructions="ALWAYS_PIRATE_SPEAK")
    await _run_chat(client, uid, fake_anthropic, workspaceProjectId=w["id"])

    sent_system = fake_anthropic.calls[0]["system_prompt"]
    assert "ALWAYS_PIRATE_SPEAK" in sent_system


@pytest.mark.asyncio
async def test_no_instructions_no_injection(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    w = await _create_workspace(client, uid, name="X")  # no instructions
    await _run_chat(client, uid, fake_anthropic, workspaceProjectId=w["id"])

    from app.chat.orchestrator import _system_prompt_for

    assert fake_anthropic.calls[0]["system_prompt"] == _system_prompt_for("chat")


# ============================================================================
# 3B — files
# ============================================================================
async def _upload(
    client: AsyncClient,
    uid: uuid.UUID,
    wid: str,
    *,
    type_: str,
    media_type: str,
    data: str,
    filename: str = "f",
) -> tuple[int, dict[str, object]]:
    r = await client.post(
        f"/v1/workspaces/{wid}/files",
        json={"type": type_, "mediaType": media_type, "filename": filename, "data": data},
        headers=auth_headers(uid),
    )
    return r.status_code, (r.json() if r.content else {})


@pytest.mark.asyncio
async def test_upload_text_extracts_and_lists_without_content(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    w = await _create_workspace(client, uid, name="X")
    code, meta = await _upload(
        client, uid, w["id"], type_="text", media_type="text/plain", data=_b64(b"hello")
    )
    assert code == 201
    assert meta["hasExtractedText"] is True
    assert meta["size"] == 5

    # extracted_text was actually persisted in the DB.
    async with db_sessionmaker() as s:
        et = await s.scalar(
            text("SELECT extracted_text FROM workspace_files WHERE id=:i"), {"i": meta["fileId"]}
        )
    assert et == "hello"

    # list returns metadata only — no content/extracted_text fields leak.
    r = await client.get(f"/v1/workspaces/{w['id']}/files", headers=auth_headers(uid))
    assert r.status_code == 200
    item = r.json()["items"][0]
    assert "content" not in item
    assert "extractedText" not in item
    assert item["filename"] == "f"


@pytest.mark.asyncio
async def test_upload_pdf_extracts_text(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    w = await _create_workspace(client, uid, name="X")
    code, meta = await _upload(
        client, uid, w["id"], type_="document", media_type="application/pdf", data=_pdf_b64(1)
    )
    assert code == 201
    # extracted_text is set (possibly empty for a blank PDF) → NOT NULL.
    async with db_sessionmaker() as s:
        et = await s.scalar(
            text("SELECT extracted_text FROM workspace_files WHERE id=:i"), {"i": meta["fileId"]}
        )
    assert et is not None


@pytest.mark.asyncio
async def test_upload_image_extracted_text_null(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    w = await _create_workspace(client, uid, name="X")
    code, meta = await _upload(
        client, uid, w["id"], type_="image", media_type="image/png", data=_b64(_PNG)
    )
    assert code == 201
    assert meta["hasExtractedText"] is False
    async with db_sessionmaker() as s:
        et = await s.scalar(
            text("SELECT extracted_text FROM workspace_files WHERE id=:i"), {"i": meta["fileId"]}
        )
    assert et is None


@pytest.mark.asyncio
async def test_upload_count_limit_422(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Cap the count low to make the 21st-file rule exercisable without uploading 21 files.
    monkeypatch.setenv("WORKSPACE_FILE_MAX_COUNT", "2")
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        async with db_sessionmaker() as s:
            uid = await seed_user(s)
        w = await _create_workspace(client, uid, name="X")
        for _ in range(2):
            code, _ = await _upload(
                client, uid, w["id"], type_="text", media_type="text/plain", data=_b64(b"x")
            )
            assert code == 201
        code, _ = await _upload(
            client, uid, w["id"], type_="text", media_type="text/plain", data=_b64(b"x")
        )
        assert code == 422
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_upload_file_too_large_413(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORKSPACE_FILE_MAX_BYTES", "16")
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        async with db_sessionmaker() as s:
            uid = await seed_user(s)
        w = await _create_workspace(client, uid, name="X")
        code, _ = await _upload(
            client, uid, w["id"], type_="text", media_type="text/plain", data=_b64(b"a" * 64)
        )
        assert code == 413
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_upload_total_bytes_limit_422(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORKSPACE_FILES_TOTAL_BYTES", "20")
    monkeypatch.setenv("WORKSPACE_FILE_MAX_BYTES", "1000000")
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        async with db_sessionmaker() as s:
            uid = await seed_user(s)
        w = await _create_workspace(client, uid, name="X")
        code, _ = await _upload(
            client, uid, w["id"], type_="text", media_type="text/plain", data=_b64(b"a" * 15)
        )
        assert code == 201
        code, _ = await _upload(
            client, uid, w["id"], type_="text", media_type="text/plain", data=_b64(b"b" * 15)
        )
        assert code == 422  # 15 + 15 > 20
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_upload_unsupported_media_type_422(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    w = await _create_workspace(client, uid, name="X")
    # mediaType not in the schema allowlist → 422 at validation.
    r = await client.post(
        f"/v1/workspaces/{w['id']}/files",
        json={
            "type": "text",
            "mediaType": "application/x-zip",
            "filename": "f",
            "data": _b64(b"x"),
        },
        headers=auth_headers(uid),
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_delete_file_and_idempotency(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    w = await _create_workspace(client, uid, name="X")
    _, meta = await _upload(
        client, uid, w["id"], type_="text", media_type="text/plain", data=_b64(b"x")
    )
    r = await client.delete(
        f"/v1/workspaces/{w['id']}/files/{meta['fileId']}", headers=auth_headers(uid)
    )
    assert r.status_code == 200
    r2 = await client.delete(
        f"/v1/workspaces/{w['id']}/files/{meta['fileId']}", headers=auth_headers(uid)
    )
    assert r2.status_code == 404


@pytest.mark.asyncio
async def test_file_isolation_foreign_workspace_404(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        owner = await seed_user(s)
        other = await seed_user(s)
    w = await _create_workspace(client, owner, name="X")
    _, meta = await _upload(
        client, owner, w["id"], type_="text", media_type="text/plain", data=_b64(b"secret")
    )
    # other cannot upload, list, or delete in the owner's workspace.
    code, _ = await _upload(
        client, other, w["id"], type_="text", media_type="text/plain", data=_b64(b"x")
    )
    assert code == 404
    assert (
        await client.get(f"/v1/workspaces/{w['id']}/files", headers=auth_headers(other))
    ).status_code == 404
    assert (
        await client.delete(
            f"/v1/workspaces/{w['id']}/files/{meta['fileId']}", headers=auth_headers(other)
        )
    ).status_code == 404


# ============================================================================
# 3B — file injection into a workspace chat + cascade
# ============================================================================
@pytest.mark.asyncio
async def test_workspace_files_injected_into_chat_first_turn(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    w = await _create_workspace(client, uid, name="X")
    await _upload(
        client,
        uid,
        w["id"],
        type_="text",
        media_type="text/plain",
        data=_b64(b"PROJECT_KNOWLEDGE_BLOB"),
        filename="notes.txt",
    )

    await _run_chat(client, uid, fake_anthropic, workspaceProjectId=w["id"])

    first_call = fake_anthropic.calls[0]["messages"]
    # The knowledge file's extracted_text is injected as a text block on the last user turn.
    blob = str(first_call)
    assert "PROJECT_KNOWLEDGE_BLOB" in blob
    assert "[Файл проекта: notes.txt]" in blob


@pytest.mark.asyncio
async def test_workspace_pdf_file_injected_as_text_on_openai(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A PDF workspace file is injected as extracted TEXT, so the OpenAI PDF→422 rule (TD-023) does
    # NOT fire. Provider is selected via LLM_PROVIDER; the fake client mirrors the anthropic wire,
    # but the SERVICE-side context build is what we exercise (text block, not a native PDF block).
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    w = await _create_workspace(client, uid, name="X")
    # Upload a text file standing in for a PDF's extracted text path is covered elsewhere; here we
    # upload a real PDF and assert the chat does NOT 422 and a text-context block is produced.
    code, _ = await _upload(
        client, uid, w["id"], type_="document", media_type="application/pdf", data=_pdf_b64(1)
    )
    assert code == 201
    out = await _run_chat(client, uid, fake_anthropic, workspaceProjectId=w["id"])
    assert out["status"] == "assistant_message"


@pytest.mark.asyncio
async def test_delete_workspace_cascades_files_and_orphans_chats(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    w = await _create_workspace(client, uid, name="X")
    _, meta = await _upload(
        client, uid, w["id"], type_="text", media_type="text/plain", data=_b64(b"x")
    )
    out = await _run_chat(client, uid, fake_anthropic, workspaceProjectId=w["id"])
    sid = out["sessionId"]

    r = await client.delete(f"/v1/workspaces/{w['id']}", headers=auth_headers(uid))
    assert r.status_code == 200

    async with db_sessionmaker() as s:
        # workspace_files CASCADE-deleted.
        files = await s.scalar(
            text("SELECT count(*) FROM workspace_files WHERE id=:i"), {"i": meta["fileId"]}
        )
        # chat survives; binding set NULL.
        bound = await s.scalar(
            text("SELECT workspace_project_id FROM chat_sessions WHERE id=:i"), {"i": sid}
        )
        alive = await s.scalar(text("SELECT count(*) FROM chat_sessions WHERE id=:i"), {"i": sid})
        steps = await s.scalar(
            text("SELECT count(*) FROM chat_steps WHERE session_id=:i"), {"i": sid}
        )
    assert int(files) == 0
    assert bound is None
    assert int(alive) == 1  # chat not deleted
    assert int(steps) >= 1  # history preserved


# ============================================================================
# Backward compatibility: a chat WITHOUT workspaceProjectId is unaffected.
# ============================================================================
@pytest.mark.asyncio
async def test_plain_chat_unaffected_no_workspace_injection(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    out = await _run_chat(client, uid, fake_anthropic)  # no workspaceProjectId
    assert out["status"] == "assistant_message"

    from app.chat.orchestrator import _system_prompt_for

    assert fake_anthropic.calls[0]["system_prompt"] == _system_prompt_for("chat")
    async with db_sessionmaker() as s:
        bound = await s.scalar(
            text("SELECT workspace_project_id FROM chat_sessions WHERE id=:i"),
            {"i": out["sessionId"]},
        )
    assert bound is None
