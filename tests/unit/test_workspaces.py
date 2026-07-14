"""Unit tests for the workspaces module (ADR-036): validation/extraction, cursor, prompt/context.

No I/O: these cover the pure logic per workspaces/09-testing.md §Unit —
- file validation + text extraction (PDF via pypdf, text/json decode, image → extracted_text=None);
- per-file size/mediaType limits surfaced as 413/422 BEFORE persistence;
- the opaque keyset cursor round-trip and rejection of garbage;
- system-prompt composition (base → instructions; empty → no injection);
- the WORKSPACE_CONTEXT_MAX_CHARS collective truncation of injected extracted_text.

Settings are built from the real Settings model with the workspace caps overridden in-memory; no
container is needed (the service context-builder operates on in-memory WorkspaceFile rows).
"""

from __future__ import annotations

import base64
import datetime
import io
import uuid

import pytest

from app.chat.attachments import PreparedAttachments
from app.config import Settings
from app.errors import PayloadTooLargeError, ValidationFailedError
from app.models import WorkspaceFile
from app.schemas.workspaces import WorkspaceFileUploadRequest
from app.workspaces.cursor import InvalidCursorError, WorkspaceCursor
from app.workspaces.service import WorkspacesService
from app.workspaces.text_extract import validate_and_extract

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _pdf_bytes(pages: int = 1, text: str | None = None) -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "KMS_LOCAL_MASTER_KEY": "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=",
        "JWT_PUBLIC_KEY": "x",
        "DATABASE_URL": "postgresql+asyncpg://x:y@localhost/z",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _upload(
    *, type_: str, media_type: str, data: bytes | str, filename: str = "f"
) -> WorkspaceFileUploadRequest:
    return WorkspaceFileUploadRequest(
        type=type_,  # type: ignore[arg-type]
        mediaType=media_type,  # type: ignore[arg-type]
        filename=filename,
        data=data if isinstance(data, str) else _b64(data),
    )


# ============================================================================
# 1. Text extraction by class
# ============================================================================
def test_text_plain_extracted_to_text() -> None:
    req = _upload(type_="text", media_type="text/plain", data=b"hello workspace")
    out = validate_and_extract(req, _settings())
    assert out.extracted_text == "hello workspace"
    assert out.content == b"hello workspace"
    assert out.size == len(b"hello workspace")


def test_json_text_extracted() -> None:
    req = _upload(type_="text", media_type="application/json", data=b'{"a": 1}')
    out = validate_and_extract(req, _settings())
    assert out.extracted_text == '{"a": 1}'


def test_invalid_json_rejected_422() -> None:
    req = _upload(type_="text", media_type="application/json", data=b"{not json")
    with pytest.raises(ValidationFailedError):
        validate_and_extract(req, _settings())


def test_invalid_utf8_text_rejected_422() -> None:
    req = _upload(type_="text", media_type="text/plain", data=b"\xff\xfe\x00bad")
    with pytest.raises(ValidationFailedError):
        validate_and_extract(req, _settings())


def test_pdf_extracts_text() -> None:
    # A blank PDF has no extractable glyphs → extracted_text is empty string, NOT None.
    req = _upload(type_="document", media_type="application/pdf", data=_pdf_bytes(2))
    out = validate_and_extract(req, _settings())
    assert out.extracted_text is not None  # document class always sets a (possibly empty) string
    assert out.content[:5] == b"%PDF-"


def test_image_extracted_text_is_none() -> None:
    req = _upload(type_="image", media_type="image/png", data=_PNG)
    out = validate_and_extract(req, _settings())
    assert out.extracted_text is None
    assert out.size == len(_PNG)


# ============================================================================
# 2. Limits + allowlist (surfaced BEFORE persistence)
# ============================================================================
def test_media_type_outside_allowlist_rejected_422() -> None:
    # type=text but a non-text mediaType → not on that class's allowlist → 422.
    with pytest.raises(ValidationFailedError):
        validate_and_extract(
            _upload(type_="text", media_type="application/pdf", data=b"x"), _settings()
        )


def test_file_over_max_bytes_rejected_413() -> None:
    big = b"a" * 2048
    req = _upload(type_="text", media_type="text/plain", data=big)
    with pytest.raises(PayloadTooLargeError):
        validate_and_extract(req, _settings(WORKSPACE_FILE_MAX_BYTES=1024))


def test_magic_byte_mismatch_rejected_422() -> None:
    # Declared image/png but bytes are not a PNG → magic-byte mismatch → 422 (anti MIME-spoof).
    req = _upload(type_="image", media_type="image/png", data=b"not a png at all")
    with pytest.raises(ValidationFailedError):
        validate_and_extract(req, _settings())


# ============================================================================
# 3. Cursor round-trip
# ============================================================================
def test_cursor_roundtrip() -> None:
    now = datetime.datetime.now(tz=datetime.UTC)
    wid = uuid.uuid4()
    token = WorkspaceCursor(updated_at=now, id=wid).encode()
    decoded = WorkspaceCursor.decode(token)
    assert decoded.id == wid
    assert decoded.updated_at == now


def test_cursor_garbage_rejected() -> None:
    with pytest.raises(InvalidCursorError):
        WorkspaceCursor.decode("!!!not-base64!!!")


def test_cursor_naive_datetime_gets_utc() -> None:
    naive = "2026-06-17T12:00:00"
    token = base64.urlsafe_b64encode(f"{naive}|{uuid.uuid4()}".encode()).decode("ascii")
    decoded = WorkspaceCursor.decode(token)
    assert decoded.updated_at.tzinfo is datetime.UTC


# ============================================================================
# 4. System-prompt composition (ADR-036 §3)
# ============================================================================
def test_system_prompt_injects_instructions_after_base() -> None:
    from app.chat.orchestrator import _system_prompt_for, _system_prompt_with_workspace

    base = _system_prompt_for("chat")
    composed = _system_prompt_with_workspace("chat", "Always answer in pirate speak.")
    assert composed.startswith(base)
    assert composed.endswith("Always answer in pirate speak.")
    assert composed != base


def test_system_prompt_empty_instructions_no_injection() -> None:
    from app.chat.orchestrator import _system_prompt_for, _system_prompt_with_workspace

    base = _system_prompt_for("chat")
    assert _system_prompt_with_workspace("chat", None) == base
    assert _system_prompt_with_workspace("chat", "") == base
    assert _system_prompt_with_workspace("chat", "   ") == base


# ============================================================================
# 5. Context truncation (WORKSPACE_CONTEXT_MAX_CHARS) — service _build_file_attachments
# ============================================================================
def _wf(
    *, media_type: str, extracted_text: str | None, content: bytes = b"", created_at: int = 0
) -> WorkspaceFile:
    return WorkspaceFile(
        id=uuid.uuid4(),
        workspace_project_id=uuid.uuid4(),
        filename="doc.txt",
        content=content,
        media_type=media_type,
        size=len(content),
        extracted_text=extracted_text,
        created_at=datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(seconds=created_at),
    )


def _svc(**overrides: object) -> WorkspacesService:
    # repository is unused by the pure _build_file_attachments path.
    return WorkspacesService(repo=None, settings=_settings(**overrides))  # type: ignore[arg-type]


def test_context_truncates_to_max_chars() -> None:
    svc = _svc(WORKSPACE_CONTEXT_MAX_CHARS=10)
    files = [_wf(media_type="text/plain", extracted_text="A" * 100)]
    prepared = svc._build_file_attachments(files, "anthropic")
    assert isinstance(prepared, PreparedAttachments)
    text_block = prepared.content_blocks[0]["text"]
    # The marker prefix plus exactly 10 chars of the body (the budget bounds extracted_text only).
    assert text_block.count("A") == 10


def test_context_drops_later_text_after_budget_exhausted() -> None:
    svc = _svc(WORKSPACE_CONTEXT_MAX_CHARS=5)
    files = [
        _wf(media_type="text/plain", extracted_text="AAAAA", created_at=0),
        _wf(media_type="text/plain", extracted_text="BBBBB", created_at=1),
    ]
    prepared = svc._build_file_attachments(files, "anthropic")
    assert prepared is not None
    joined = "".join(str(b.get("text", "")) for b in prepared.content_blocks)
    assert "A" in joined
    assert "B" not in joined  # second file dropped — budget already consumed by the first


def test_context_images_not_counted_against_char_budget() -> None:
    svc = _svc(WORKSPACE_CONTEXT_MAX_CHARS=0)
    files = [
        _wf(media_type="text/plain", extracted_text="hello"),
        _wf(media_type="image/png", extracted_text=None, content=_PNG),
    ]
    prepared = svc._build_file_attachments(files, "anthropic")
    assert prepared is not None
    # No text budget → text dropped, but the image vision block is still present.
    types = [b.get("type") for b in prepared.content_blocks]
    assert "image" in types
    assert "text" not in types


def test_context_image_block_provider_agnostic() -> None:
    # Prod-bug fix: the OpenAI branch must emit a Responses `input_image` part with a FLAT
    # `image_url` data-URI string + `detail`, NOT the old Chat Completions `{type:image_url,
    # image_url:{url:...}}` (which the Responses API rejected with 400 → 502 upstream_error).
    svc = _svc()
    files = [_wf(media_type="image/png", extracted_text=None, content=_PNG)]

    anthropic = svc._build_file_attachments(files, "anthropic")
    assert anthropic is not None
    assert anthropic.content_blocks[0]["type"] == "image"
    assert anthropic.content_blocks[0]["source"]["type"] == "base64"

    openai = svc._build_file_attachments(files, "openai")
    assert openai is not None
    block = openai.content_blocks[0]
    assert block["type"] == "input_image"
    # image_url is a PLAIN string data-URI, NOT a nested {"url": ...} object.
    assert isinstance(block["image_url"], str)
    assert str(block["image_url"]).startswith("data:image/png;base64,")
    assert block["detail"] == "auto"


def test_context_pdf_extracted_text_injected_as_text_both_providers() -> None:
    # A workspace PDF carries extracted_text → it is injected as a TEXT block (not native PDF),
    # so the OpenAI PDF→422 rule (TD-023) does NOT apply to workspace files. Prod-bug fix: the
    # OpenAI text part is `input_text` (Responses), Anthropic keeps `text` (ADR-059 §6).
    svc = _svc()
    files = [_wf(media_type="application/pdf", extracted_text="page text", content=b"%PDF-")]
    expected_type = {"anthropic": "text", "openai": "input_text"}
    for provider in ("anthropic", "openai"):
        prepared = svc._build_file_attachments(files, provider)
        assert prepared is not None, provider
        block = prepared.content_blocks[0]
        assert block["type"] == expected_type[provider], provider
        assert "page text" in str(block["text"])
        assert "[Файл проекта: doc.txt]" in str(block["text"])


def test_context_no_injectable_files_returns_none() -> None:
    svc = _svc()
    # An image-only file with extracted_text None still yields a block; an empty-text doc does not.
    assert svc._build_file_attachments([], "anthropic") is None
    files = [_wf(media_type="text/plain", extracted_text="")]
    assert svc._build_file_attachments(files, "anthropic") is None


# ============================================================================
# 6. Prod-bug fix (workspace knowledge-file injection wire format, ADR-059 §6)
#    OpenAI blocks MUST be Responses parts (input_text / input_image + flat
#    image_url + detail), NOT Chat Completions parts (which returned 400 → 502).
# ============================================================================
def test_build_file_attachments_openai_text_is_input_text() -> None:
    # Scenario 1: a text WorkspaceFile on provider="openai" yields an `input_text` part,
    # NOT the old `{type:"text"}` (rejected by the Responses API).
    svc = _svc()
    files = [_wf(media_type="text/plain", extracted_text="hello workspace")]
    prepared = svc._build_file_attachments(files, "openai")
    assert prepared is not None
    block = prepared.content_blocks[0]
    assert block["type"] == "input_text"
    assert block["type"] != "text"
    assert "hello workspace" in str(block["text"])
    assert str(block["text"]).startswith("[Файл проекта: doc.txt]\n")


def test_image_block_openai_is_responses_input_image() -> None:
    # Scenario 2: `_image_block` on provider="openai" builds `input_image` with a FLAT string
    # image_url (data:...) and detail=="auto"; NO Chat Completions `{type:"image_url"}` and NO
    # nested `{url: ...}` object.
    f = _wf(media_type="image/png", extracted_text=None, content=_PNG)
    block = WorkspacesService._image_block(f, "openai")
    assert block is not None
    assert block["type"] == "input_image"
    assert block["type"] != "image_url"  # not the old Chat Completions shape
    assert isinstance(block["image_url"], str)  # flat string, not {"url": ...}
    assert str(block["image_url"]).startswith("data:image/png;base64,")
    assert block.get("detail") == "auto"


def test_build_file_attachments_anthropic_unchanged() -> None:
    # Scenario 3: provider="anthropic" is a regression check — text stays `{type:"text"}` and
    # images stay the native `{type:"image", source:{...}}` block (fix must not touch Anthropic).
    svc = _svc()
    files = [
        _wf(media_type="text/plain", extracted_text="doc body"),
        _wf(media_type="image/png", extracted_text=None, content=_PNG),
    ]
    prepared = svc._build_file_attachments(files, "anthropic")
    assert prepared is not None
    text_block, image_block = prepared.content_blocks
    assert text_block["type"] == "text"
    assert "doc body" in str(text_block["text"])
    assert image_block["type"] == "image"
    assert image_block["source"]["type"] == "base64"
    assert image_block["source"]["media_type"] == "image/png"
    assert isinstance(image_block["source"]["data"], str)


def test_openai_workspace_blocks_match_attachments_reference_shape() -> None:
    # Scenario 4: the OpenAI workspace blocks match, key-for-key, the SSOT reference shape built by
    # attachments.py `_openai_content_block` (input_text keys / input_image + flat image_url +
    # detail). Guards against future drift between the two OpenAI content-block builders.
    from app.chat.attachments import _openai_content_block
    from app.schemas.chat import AttachmentIn

    svc = _svc()

    # --- image parity ---------------------------------------------------------------------
    ws_image = WorkspacesService._image_block(
        _wf(media_type="image/png", extracted_text=None, content=_PNG), "openai"
    )
    assert ws_image is not None
    ref_image = _openai_content_block(
        AttachmentIn(type="image", mediaType="image/png", filename="x.png", data=_b64(_PNG)),
        _PNG,
        None,
    )
    # Same key set and same non-payload shape (type / flat image_url string / detail).
    assert set(ws_image.keys()) == set(ref_image.keys()) == {"type", "image_url", "detail"}
    assert ws_image["type"] == ref_image["type"] == "input_image"
    assert isinstance(ws_image["image_url"], str) and isinstance(ref_image["image_url"], str)
    assert str(ws_image["image_url"]).startswith("data:image/png;base64,")
    assert str(ref_image["image_url"]).startswith("data:image/png;base64,")
    assert ws_image["detail"] == ref_image["detail"] == "auto"

    # --- text parity (part type) ----------------------------------------------------------
    ws_text = svc._build_file_attachments(
        [_wf(media_type="text/plain", extracted_text="body")], "openai"
    )
    assert ws_text is not None
    ref_text = _openai_content_block(
        AttachmentIn(type="text", mediaType="text/plain", filename="x.txt", data=_b64(b"body")),
        b"body",
        "body",
    )
    assert ws_text.content_blocks[0]["type"] == ref_text["type"] == "input_text"
    assert set(ws_text.content_blocks[0].keys()) == set(ref_text.keys()) == {"type", "text"}
