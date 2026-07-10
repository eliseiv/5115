"""Unit tests for inline base64 attachment validation + Anthropic mapping (ADR-020).

Covers the attachment threat model (05-security.md) and the 06-testing-strategy.md ADR-020 cases
at the unit level: allowlist, size/count limits BEFORE decode, base64 validity, magic-byte/UTF-8/
JSON consistency (anti-spoof), PDF page-guard (anti-bomb), content-block wire mapping (image /
document-dict / text), and the storage invariant (placeholders carry no base64). All rejections
must be ValidationFailedError (-> 422), never a 500.
"""

from __future__ import annotations

import base64
import io
import json

import pytest

from app.chat.attachments import _decoded_len_from_base64, prepare_attachments
from app.config import Settings
from app.errors import ValidationFailedError
from app.schemas.chat import AttachmentIn

# ----------------------------- byte fixtures -----------------------------
# Minimal valid magic-byte payloads for each image class.
_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 16
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
_GIF = b"GIF89a" + b"\x00" * 16
_WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 16


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _pdf_bytes(pages: int = 1, *, encrypt: str | None = None) -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=72, height=72)
    if encrypt is not None:
        writer.encrypt(encrypt)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _att(
    att_type: str, media_type: str, data: bytes | str, *, filename: str | None = None
) -> AttachmentIn:
    raw = data if isinstance(data, str) else _b64(data)
    return AttachmentIn(type=att_type, mediaType=media_type, filename=filename, data=raw)  # type: ignore[arg-type]


@pytest.fixture
def settings() -> Settings:
    # Defaults match config.py (image=5MB, document=8MB, total=10MB, count=10, pdf_pages=100).
    return Settings()


# --- scenario 1: image classes -> image block ---
@pytest.mark.parametrize(
    ("media_type", "payload"),
    [
        ("image/jpeg", _JPEG),
        ("image/png", _PNG),
        ("image/gif", _GIF),
        ("image/webp", _WEBP),
    ],
)
def test_image_attachment_maps_to_image_block(
    media_type: str, payload: bytes, settings: Settings
) -> None:
    att = _att("image", media_type, payload, filename="p.img")
    prepared = prepare_attachments([att], settings)

    assert len(prepared.content_blocks) == 1
    block = prepared.content_blocks[0]
    assert block["type"] == "image"
    source = block["source"]
    assert isinstance(source, dict)
    assert source["type"] == "base64"
    assert source["media_type"] == media_type
    # The block carries the ORIGINAL base64 string verbatim (sent to Anthropic in-memory).
    assert source["data"] == att.data


# --- scenario 1: PDF -> native document-dict block ---
def test_pdf_attachment_maps_to_document_dict_block(settings: Settings) -> None:
    att = _att("document", "application/pdf", _pdf_bytes(1), filename="doc.pdf")
    prepared = prepare_attachments([att], settings)

    block = prepared.content_blocks[0]
    # TD-016: anthropic 0.39.0 has no DocumentBlockParam -> emitted as the raw wire-format dict.
    assert block == {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": "application/pdf",
            "data": att.data,
        },
    }


def test_pdf_text_is_not_extracted(settings: Settings) -> None:
    # ADR-020: PDF goes natively to a document block; backend never extracts text from it.
    att = _att("document", "application/pdf", _pdf_bytes(1))
    block = prepare_attachments([att], settings).content_blocks[0]
    assert block["type"] == "document"
    assert "text" not in block  # no extracted_text path


# --- scenario 1: text classes -> text block with filename markup ---
@pytest.mark.parametrize(
    "media_type",
    ["text/plain", "text/markdown", "text/csv", "application/json"],
)
def test_text_attachment_maps_to_text_block_with_filename(
    media_type: str, settings: Settings
) -> None:
    content = '{"a":1}' if media_type == "application/json" else "hello, world"
    att = _att("text", media_type, content.encode("utf-8"), filename="notes.txt")
    block = prepare_attachments([att], settings).content_blocks[0]
    assert block["type"] == "text"
    text = block["text"]
    assert isinstance(text, str)
    assert "notes.txt" in text  # explicit filename markup
    assert content in text  # decoded content inlined in a fenced block
    assert "```" in text


def test_text_attachment_without_filename_uses_default_name(settings: Settings) -> None:
    att = _att("text", "text/plain", b"body")
    block = prepare_attachments([att], settings).content_blocks[0]
    assert block["text"].startswith("file\n```")


# ----------------------------- scenario 2: magic-byte spoof -----------------------------
def test_magic_byte_spoof_png_declared_but_not_png_rejected(settings: Settings) -> None:
    # Declared image/png, body is not a PNG (JPEG magic) -> 422.
    att = _att("image", "image/png", _JPEG)
    with pytest.raises(ValidationFailedError):
        prepare_attachments([att], settings)


def test_magic_byte_spoof_pdf_declared_but_not_pdf_rejected(settings: Settings) -> None:
    att = _att("document", "application/pdf", b"%NOTPDF" + b"\x00" * 32)
    with pytest.raises(ValidationFailedError):
        prepare_attachments([att], settings)


def test_webp_without_webp_fourcc_rejected(settings: Settings) -> None:
    # RIFF container but missing the WEBP fourcc at offset 8 -> spoof -> 422.
    bad = b"RIFF" + b"\x00\x00\x00\x00" + b"AVI " + b"\x00" * 16
    att = _att("image", "image/webp", bad)
    with pytest.raises(ValidationFailedError):
        prepare_attachments([att], settings)


# --- scenario 2: invalid base64 / UTF-8 / JSON ---
def test_invalid_base64_rejected_as_422_not_500(settings: Settings) -> None:
    # '@@@@' is not valid base64 alphabet -> ValidationFailedError (422), never a 500.
    att = AttachmentIn(type="image", mediaType="image/png", data="@@@@not-base64@@@@")
    with pytest.raises(ValidationFailedError):
        prepare_attachments([att], settings)


def test_truncated_base64_rejected_as_422(settings: Settings) -> None:
    # Drop a char so length % 4 != 0 -> binascii.Error -> 422.
    valid = _b64(_PNG)
    att = AttachmentIn(type="image", mediaType="image/png", data=valid[:-1])
    with pytest.raises(ValidationFailedError):
        prepare_attachments([att], settings)


def test_text_invalid_utf8_rejected(settings: Settings) -> None:
    # 0xff 0xfe is not valid UTF-8 -> 422.
    att = _att("text", "text/plain", b"\xff\xfe\xfa")
    with pytest.raises(ValidationFailedError):
        prepare_attachments([att], settings)


def test_json_invalid_payload_rejected(settings: Settings) -> None:
    # Valid UTF-8 but not valid JSON, declared application/json -> 422.
    att = _att("text", "application/json", b"{not valid json")
    with pytest.raises(ValidationFailedError):
        prepare_attachments([att], settings)


def test_json_valid_payload_accepted(settings: Settings) -> None:
    att = _att("text", "application/json", json.dumps({"k": "v"}).encode("utf-8"))
    block = prepare_attachments([att], settings).content_blocks[0]
    assert block["type"] == "text"


# ----------------------------- scenario 3: MIME outside allowlist -----------------------------
def test_mime_outside_allowlist_rejected_at_schema_level() -> None:
    # The Literal allowlist is enforced by Pydantic before the orchestrator -> ValidationError.
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AttachmentIn(type="image", mediaType="application/zip", data="AAAA")  # type: ignore[arg-type]


def test_class_mediatype_mismatch_rejected(settings: Settings) -> None:
    # mediaType on the global allowlist but wrong for the declared class (pdf under image) -> 422.
    att = AttachmentIn(type="image", mediaType="application/pdf", data=_b64(_PNG))
    with pytest.raises(ValidationFailedError):
        prepare_attachments([att], settings)


# ----------------------------- scenario 4: limits BEFORE decode -----------------------------
def test_single_image_over_limit_rejected_before_decode(settings: Settings) -> None:
    small = Settings(ATTACHMENT_MAX_BYTES_IMAGE=1024)
    # ~2KB decoded > 1KB limit. Use a base64 string long enough; content correctness is irrelevant
    # because the size check happens BEFORE decode/magic-byte.
    big_b64 = _b64(b"\x00" * 4096)
    att = AttachmentIn(type="image", mediaType="image/png", data=big_b64)
    with pytest.raises(ValidationFailedError):
        prepare_attachments([att], small)


def test_total_size_over_limit_rejected(settings: Settings) -> None:
    small = Settings(ATTACHMENT_MAX_BYTES_IMAGE=8192, ATTACHMENT_TOTAL_BYTES=4096)
    a1 = AttachmentIn(type="image", mediaType="image/png", data=_b64(b"\x00" * 3000))
    a2 = AttachmentIn(type="image", mediaType="image/png", data=_b64(b"\x00" * 3000))
    with pytest.raises(ValidationFailedError):
        prepare_attachments([a1, a2], small)


def test_too_many_attachments_rejected(settings: Settings) -> None:
    small = Settings(ATTACHMENT_MAX_COUNT=2)
    atts = [_att("image", "image/png", _PNG) for _ in range(3)]
    with pytest.raises(ValidationFailedError):
        prepare_attachments(atts, small)


def test_size_check_is_before_decode_huge_input_not_decoded(settings: Settings) -> None:
    """Anti memory-DoS: an oversized base64 input must be rejected from its LENGTH alone, never
    fully b64decoded. We assert the cheap upper-bound estimator rejects it without allocating the
    decoded buffer; the estimator (3/4 of the b64 length) is the gate used before b64decode.
    """
    small = Settings(ATTACHMENT_MAX_BYTES_IMAGE=1024)
    # 1M base64 chars -> ~768KB decoded estimate, far over 1KB. The estimator must flag it.
    huge_b64 = "A" * 1_000_000
    assert _decoded_len_from_base64(huge_b64) > small.attachment_max_bytes_image
    att = AttachmentIn(type="image", mediaType="image/png", data=huge_b64)
    with pytest.raises(ValidationFailedError):
        prepare_attachments([att], small)


def test_decoded_len_estimator_matches_actual() -> None:
    for n in (0, 1, 10, 100, 999):
        raw = b"\x01" * n
        encoded = _b64(raw)
        assert _decoded_len_from_base64(encoded) == n


# --- scenario 5: PDF page-guard / encrypted / corrupt ---
def test_pdf_over_page_limit_rejected(settings: Settings) -> None:
    small = Settings(ATTACHMENT_PDF_MAX_PAGES=2)
    att = _att("document", "application/pdf", _pdf_bytes(3))
    with pytest.raises(ValidationFailedError):
        prepare_attachments([att], small)


def test_pdf_at_page_limit_accepted(settings: Settings) -> None:
    small = Settings(ATTACHMENT_PDF_MAX_PAGES=3)
    att = _att("document", "application/pdf", _pdf_bytes(3))
    prepared = prepare_attachments([att], small)
    assert prepared.content_blocks[0]["type"] == "document"


def test_encrypted_pdf_rejected(settings: Settings) -> None:
    att = _att("document", "application/pdf", _pdf_bytes(1, encrypt="secret"))
    with pytest.raises(ValidationFailedError):
        prepare_attachments([att], settings)


def test_corrupt_pdf_rejected_not_crash(settings: Settings) -> None:
    # Valid %PDF- magic so magic-byte passes, but the body is garbage -> pypdf parse error -> 422
    # (never an unhandled crash / 500).
    att = _att("document", "application/pdf", b"%PDF-1.4\nthis is not a real pdf body\n%%EOF")
    with pytest.raises(ValidationFailedError):
        prepare_attachments([att], settings)


# ----------------------------- scenario 7: storage invariant (placeholders, no base64) ------------
def test_placeholders_contain_no_base64(settings: Settings) -> None:
    img = _att("image", "image/png", _PNG, filename="a.png")
    pdf = _att("document", "application/pdf", _pdf_bytes(1), filename="b.pdf")
    txt = _att("text", "text/plain", b"hi", filename="c.txt")
    prepared = prepare_attachments([img, pdf, txt], settings)

    assert len(prepared.placeholders) == 3
    for ph, att in zip(prepared.placeholders, [img, pdf, txt], strict=True):
        assert ph["type"] == "text"
        # The raw base64 data MUST NOT appear in any persisted placeholder (storage invariant).
        assert att.data not in ph["text"]
        assert att.mediaType in ph["text"]
        assert "attachment" in ph["text"]


def test_placeholders_count_matches_content_blocks(settings: Settings) -> None:
    atts = [_att("image", "image/png", _PNG), _att("text", "text/plain", b"x")]
    prepared = prepare_attachments(atts, settings)
    assert len(prepared.placeholders) == len(prepared.content_blocks) == 2


# ----------------------------- empty list -----------------------------
def test_empty_attachments_yield_empty(settings: Settings) -> None:
    prepared = prepare_attachments([], settings)
    assert prepared.content_blocks == []
    assert prepared.placeholders == []
