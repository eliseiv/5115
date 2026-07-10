"""Inline base64 multimodal attachment validation and per-provider content-block mapping.

Implements ADR-020 (inline base64 attachments MVP), the per-provider mapping of ADR-033, and the
attachment threat model in 05-security.md. Attachments arrive inline in the first user message-step
of /v1/chat/run.

Validation pipeline per attachment (order matters — limits BEFORE decode; SHARED across providers,
ADR-033 §5):
1. mediaType is on the fixed allowlist for its class (else 422 unsupported_media_type);
2. base64-string length implies a decoded size within the per-class and total byte limits
   (checked BEFORE b64decode to bound memory — anti memory-DoS);
3. base64 is well-formed (else 422, never 500);
4. decoded content matches the declared mediaType by magic bytes / UTF-8 / JSON parse
   (anti MIME-spoof — never trust the client's mediaType);
5. PDF: page-count guard via pypdf (anti decompression/structure bomb).

The content-block mapping is PROVIDER-AWARE (ADR-033 §5), branched AFTER the shared validation:
- ``anthropic`` (default): image ``{type:image,source:{type:base64,...}}``, document(PDF)
  ``{type:document,...}``, text — as before;
- ``openai`` (Responses API, ADR-059 §6): image → ``{type:input_image, image_url:"data:<mediaType>;
  base64,<data>", detail:"auto"}``, text → ``{type:input_text, text:...}``, and **PDF → native
  ``input_file`` part** ``{type:input_file, filename, file_data:"data:application/pdf;base64,
  <data>"}`` (ADR-041 intent preserved — OpenAI vision reads PDF; new Responses transport).

The validated attachments are mapped to content blocks IN MEMORY for the first messages.create call
only. Raw base64 is NEVER persisted: chat_steps.payload stores a light text placeholder instead
(ADR-020 §3 storage invariant; placeholders are provider-agnostic). The Anthropic PDF document-block
is emitted as a raw dict per the wire format because anthropic 0.39.0 has no DocumentBlockParam
(TD-016); the backend already sends messages as raw dicts.

Raises ValidationFailedError (-> 422) for every rejection so attachment errors are technical
validation failures, never 500s. Attachment bytes/text never reach logs (redaction is upstream;
this module never logs content).
"""

from __future__ import annotations

import base64
import binascii
import io
import json
from dataclasses import dataclass

from app.config import Settings
from app.errors import ValidationFailedError
from app.schemas.chat import AttachmentIn

# --- mediaType allowlist per class (fixed in code; Q-020-1 governs extension) ---------------
_IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})
_DOCUMENT_TYPES = frozenset({"application/pdf"})
_TEXT_TYPES = frozenset({"text/plain", "text/markdown", "text/csv", "application/json"})

_ALLOWLIST: dict[str, frozenset[str]] = {
    "image": _IMAGE_TYPES,
    "document": _DOCUMENT_TYPES,
    "text": _TEXT_TYPES,
}

# Magic-byte signatures for image/PDF classes. WEBP is "RIFF"...."WEBP" (offset 8).
_MAGIC_PREFIXES: dict[str, tuple[bytes, ...]] = {
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/gif": (b"GIF87a", b"GIF89a"),
    "application/pdf": (b"%PDF-",),
}


# Provider identifiers for the per-provider content-block builder (ADR-033 §5).
PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_OPENAI = "openai"


@dataclass(frozen=True)
class PreparedAttachments:
    """Result of validating a request's attachments (ADR-020 / ADR-033 §5).

    - content_blocks: provider content blocks (built for the ACTIVE provider) for the FIRST
      messages.create call only — full base64 in memory, never persisted.
    - placeholders: light text blocks persisted in chat_steps.payload INSTEAD of base64
      (storage invariant: raw base64 is never stored; provider-agnostic).
    """

    content_blocks: list[dict[str, object]]
    placeholders: list[dict[str, str]]


def _max_bytes_for(attachment_type: str, settings: Settings) -> int:
    if attachment_type == "document":
        return settings.attachment_max_bytes_document
    # image and text share the image ceiling; text files are small inline inputs.
    return settings.attachment_max_bytes_image


def _decoded_len_from_base64(data: str) -> int:
    """Upper bound of the decoded byte length from a base64 string, BEFORE decoding.

    base64 encodes 3 bytes per 4 chars; the decoded size is (len/4)*3 minus padding. This is
    used to reject oversized payloads without ever allocating the decoded buffer (anti DoS).
    """
    stripped = data.strip()
    n = len(stripped)
    if n == 0:
        return 0
    padding = stripped.count("=", max(0, n - 2))
    return (n // 4) * 3 - padding


def _decode_base64(data: str) -> bytes:
    try:
        # validate=True rejects non-alphabet characters (truncated/garbage -> 422, not 500).
        return base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValidationFailedError("attachment data is not valid base64") from exc


def _check_magic_bytes(media_type: str, decoded: bytes) -> None:
    if media_type == "image/webp":
        # RIFF container with a WEBP fourcc at offset 8.
        if not (decoded[:4] == b"RIFF" and decoded[8:12] == b"WEBP"):
            raise ValidationFailedError("attachment content does not match declared mediaType")
        return
    prefixes = _MAGIC_PREFIXES.get(media_type)
    if prefixes is None:  # pragma: no cover - allowlist guarantees a known image/pdf type here
        return
    if not any(decoded.startswith(prefix) for prefix in prefixes):
        raise ValidationFailedError("attachment content does not match declared mediaType")


def _decode_text(media_type: str, decoded: bytes) -> str:
    try:
        text = decoded.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationFailedError("text attachment is not valid UTF-8") from exc
    if media_type == "application/json":
        try:
            json.loads(text)
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValidationFailedError(
                "attachment content does not match declared mediaType"
            ) from exc
    return text


def _check_pdf_pages(decoded: bytes, settings: Settings) -> None:
    """Guard PDF page count (anti decompression/structure bomb) via pypdf — no full render.

    A malformed or password-protected PDF is rejected as 422 (suspicious structure).
    """
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(decoded))
        if reader.is_encrypted:
            raise ValidationFailedError("password-protected PDF is not accepted")
        pages = len(reader.pages)
    except ValidationFailedError:
        raise
    except (PdfReadError, ValueError, OSError) as exc:
        raise ValidationFailedError("PDF could not be parsed") from exc
    if pages > settings.attachment_pdf_max_pages:
        raise ValidationFailedError("PDF exceeds the maximum allowed number of pages")


def _placeholder(att: AttachmentIn, decoded_size: int) -> dict[str, str]:
    name = att.filename or "file"
    return {
        "type": "text",
        "text": (
            f'[attachment: {att.mediaType} "{name}", {decoded_size}B '
            f"— отправлено в первом обращении к модели]"
        ),
    }


def _text_block(att: AttachmentIn, text: str | None) -> dict[str, object]:
    # text: inline the decoded UTF-8 with an explicit filename and a fenced code block. Same
    # representation for both providers (a plain text block).
    name = att.filename or "file"
    body = text if text is not None else ""
    return {"type": "text", "text": f"{name}\n```\n{body}\n```"}


def _anthropic_content_block(
    att: AttachmentIn, decoded: bytes, text: str | None
) -> dict[str, object]:
    if att.type == "image":
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": att.mediaType, "data": att.data},
        }
    if att.type == "document":
        # TD-016: anthropic 0.39.0 has no DocumentBlockParam — emit the raw wire-format dict.
        return {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": att.data,
            },
        }
    return _text_block(att, text)


def _openai_content_block(att: AttachmentIn, decoded: bytes, text: str | None) -> dict[str, object]:
    """OpenAI Responses input content-part (ADR-059 §6, revises ADR-041 transport).

    image → ``input_image`` with a base64 data-URI; text → ``input_text`` part; document (PDF) →
    native ``input_file`` part. This is the Responses wire format — it REPLACES the former Chat
    Completions shapes (``image_url`` / ``file`` / ``text``) that ADR-041 used (ADR-059 §6). The
    Anthropic branch is untouched.

    Confirmed by introspection of pinned SDK 1.109.1:
    - ``ResponseInputImageParam``: ``type:'input_image'`` + ``image_url`` as a PLAIN string (the
      data-URI), plus required ``detail`` (``'auto'`` here) — NOT a nested ``{url: ...}`` object;
    - ``ResponseInputFileParam``: ``type:'input_file'`` + ``filename`` + ``file_data`` (data-URI),
      so OpenAI vision reads the PDF natively (ADR-041 §1 intent preserved, new transport);
    - ``ResponseInputTextParam``: ``type:'input_text'`` + ``text``.

    Raw base64 lives in-memory for the first Responses call only — never persisted (placeholder,
    ADR-020 §3 / ADR-041 §5).
    """
    if att.type == "image":
        return {
            "type": "input_image",
            "image_url": f"data:{att.mediaType};base64,{att.data}",
            "detail": "auto",
        }
    if att.type == "document":
        # ADR-059 §6: native Responses file-input part (was Chat Completions ``file``).
        filename = att.filename or "file"
        return {
            "type": "input_file",
            "filename": filename,
            "file_data": f"data:{att.mediaType};base64,{att.data}",
        }
    # text → an ``input_text`` part (Responses user content requires input_* part types).
    name = att.filename or "file"
    body = text if text is not None else ""
    return {"type": "input_text", "text": f"{name}\n```\n{body}\n```"}


def _content_block(
    att: AttachmentIn, decoded: bytes, text: str | None, provider: str
) -> dict[str, object]:
    """Build the provider content block for one validated attachment (ADR-033 §5).

    Called only AFTER the shared validation. The provider branch is the single place where
    image/document/text are mapped to the provider wire format; PDF on OpenAI is a native
    ``file`` content-part here (ADR-041), not a rejection.
    """
    if provider == PROVIDER_OPENAI:
        return _openai_content_block(att, decoded, text)
    return _anthropic_content_block(att, decoded, text)


def prepare_attachments(
    attachments: list[AttachmentIn],
    settings: Settings,
    provider: str = PROVIDER_ANTHROPIC,
) -> PreparedAttachments:
    """Validate inline attachments and build provider content blocks + storage placeholders.

    Enforces (ADR-020 / 05-security.md): mediaType allowlist, size/count limits BEFORE decode,
    base64 validity, magic-byte/UTF-8/JSON consistency, PDF page-guard — ALL shared across providers
    and run BEFORE the provider branch (ADR-033 §5). The provider then selects the content-block
    shape: ``anthropic`` (default — backward compatible) builds the native image/document/text
    blocks; ``openai`` builds Responses ``input_image``/``input_text`` parts and a native
    ``input_file`` part for PDF (ADR-059 §6 — PDF supported, no longer a 422). Raises
    ValidationFailedError (-> 422) on any
    violation. Never logs attachment content.
    """
    if len(attachments) > settings.attachment_max_count:
        raise ValidationFailedError("too many attachments")

    content_blocks: list[dict[str, object]] = []
    placeholders: list[dict[str, str]] = []
    total_decoded = 0

    for att in attachments:
        allowed = _ALLOWLIST.get(att.type)
        # type is constrained by the schema Literal; defensive guard keeps mypy/logic explicit.
        if allowed is None or att.mediaType not in allowed:  # pragma: no branch
            raise ValidationFailedError(f"unsupported_media_type: {att.mediaType}")

        # Limits BEFORE base64 decode (anti memory-DoS): bound decoded size from the b64 length.
        approx_decoded = _decoded_len_from_base64(att.data)
        if approx_decoded > _max_bytes_for(att.type, settings):
            raise ValidationFailedError("attachment exceeds the maximum size")
        total_decoded += approx_decoded
        if total_decoded > settings.attachment_total_bytes:
            raise ValidationFailedError("attachments exceed the total size limit")

        decoded = _decode_base64(att.data)
        text: str | None = None
        if att.type == "image":
            _check_magic_bytes(att.mediaType, decoded)
        elif att.type == "document":
            _check_magic_bytes(att.mediaType, decoded)
            _check_pdf_pages(decoded, settings)
        else:  # text
            text = _decode_text(att.mediaType, decoded)

        # Provider branch (AFTER shared validation): per-provider PDF mapping lands here
        # (anthropic document-block / openai native file-part, ADR-041), never a 500.
        content_blocks.append(_content_block(att, decoded, text, provider))
        placeholders.append(_placeholder(att, len(decoded)))

    return PreparedAttachments(content_blocks=content_blocks, placeholders=placeholders)
