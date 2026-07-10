"""Workspace knowledge-file validation + text extraction at upload (ADR-036 §4, workspaces/03).

Reuses the inline-base64 attachment validation primitives (``app.chat.attachments``) so workspace
uploads enforce the SAME threat model as chat attachments (ADR-020): mediaType allowlist, size
limits BEFORE base64 decode (anti memory-DoS), base64 validity, magic-byte / UTF-8 / JSON
consistency (anti MIME-spoof) and the PDF page-count guard (anti decompression/structure bomb).

Difference from chat attachments: the decoded bytes are PERSISTED in ``workspace_files.content``
(long-lived project context, BYTEA — TD-027) and the extracted text is computed once at upload and
stored in ``workspace_files.extracted_text`` (provider-agnostic context injection — ADR-036 §6).
For images ``extracted_text`` is NULL (injected as a vision block later).

Raises ValidationFailedError (-> 422) on any rejection; never logs file content (05-security.md).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.chat.attachments import (
    _check_magic_bytes,
    _check_pdf_pages,
    _decode_base64,
    _decode_text,
    _decoded_len_from_base64,
)
from app.config import Settings
from app.errors import PayloadTooLargeError, ValidationFailedError
from app.schemas.workspaces import WorkspaceFileUploadRequest

# mediaType allowlist per class (fixed in code; same as chat attachments — Q-020-1).
_IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})
_DOCUMENT_TYPES = frozenset({"application/pdf"})
_TEXT_TYPES = frozenset({"text/plain", "text/markdown", "text/csv", "application/json"})

_ALLOWLIST: dict[str, frozenset[str]] = {
    "image": _IMAGE_TYPES,
    "document": _DOCUMENT_TYPES,
    "text": _TEXT_TYPES,
}


@dataclass(frozen=True)
class ExtractedFile:
    """A validated workspace file ready to persist (ADR-036 §4).

    - ``content``: raw decoded bytes (stored in ``workspace_files.content``);
    - ``extracted_text``: decoded text for document/text, or None for images;
    - ``size``: decoded byte length (stored in ``workspace_files.size``).
    """

    content: bytes
    extracted_text: str | None
    size: int


def _extract_pdf_text(decoded: bytes) -> str:
    """Extract text from a (already page-guarded) PDF via pypdf (ADR-036 §4, TD-004a CPU caveat).

    Returns the per-page text joined by blank lines. A page with no extractable text yields an
    empty string for that page; the overall result may be empty for image-only/scanned PDFs.
    """
    import io

    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(decoded))
        parts = [page.extract_text() or "" for page in reader.pages]
    except (PdfReadError, ValueError, OSError) as exc:
        raise ValidationFailedError("PDF could not be parsed") from exc
    return "\n\n".join(p for p in parts if p).strip()


def validate_and_extract(req: WorkspaceFileUploadRequest, settings: Settings) -> ExtractedFile:
    """Validate one workspace upload and extract its text (ADR-036 §4).

    Pipeline (order matters — limits BEFORE decode):
    1. mediaType is on the fixed allowlist for its class (else 422 unsupported_media_type);
    2. decoded size (bounded from the base64 length) ≤ WORKSPACE_FILE_MAX_BYTES (else 413);
    3. base64 well-formed (else 422);
    4. magic-byte / UTF-8 / JSON consistency by class (anti MIME-spoof);
    5. PDF: page-count guard + text extraction; text/*: UTF-8 (+JSON) decode → extracted_text;
       image: extracted_text = None (vision).
    """
    allowed = _ALLOWLIST.get(req.type)
    if allowed is None or req.mediaType not in allowed:
        raise ValidationFailedError(f"unsupported_media_type: {req.mediaType}")

    # Per-file size cap BEFORE decode (anti memory-DoS): bound decoded size from the b64 length.
    approx_decoded = _decoded_len_from_base64(req.data)
    if approx_decoded > settings.workspace_file_max_bytes:
        raise PayloadTooLargeError("file exceeds the maximum allowed size")

    decoded = _decode_base64(req.data)
    # Exact-size re-check after decode (the approximation is an upper bound; enforce the real size).
    if len(decoded) > settings.workspace_file_max_bytes:
        raise PayloadTooLargeError("file exceeds the maximum allowed size")

    extracted_text: str | None = None
    if req.type == "image":
        _check_magic_bytes(req.mediaType, decoded)
    elif req.type == "document":
        _check_magic_bytes(req.mediaType, decoded)
        _check_pdf_pages(decoded, settings)
        extracted_text = _extract_pdf_text(decoded)
    else:  # text
        extracted_text = _decode_text(req.mediaType, decoded)

    return ExtractedFile(content=decoded, extracted_text=extracted_text, size=len(decoded))
