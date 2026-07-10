"""Unit tests: attachment redaction (05-security.md) + schema guards (ADR-020).

- redaction: attachments[].data and decoded content never survive into logs/audit;
- schema: AttachmentIn is StrictModel (extra forbidden), only base64 (no url source),
  and ChatToolResultRequest does NOT accept attachments (extra='forbid').
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.observability.redaction import REDACTED, redact
from app.schemas.chat import AttachmentIn, ChatToolResultRequest


# --- scenario 8: redaction of attachment data ---
def test_redact_attachments_data_field() -> None:
    payload = {
        "attachments": [
            {"type": "image", "mediaType": "image/png", "filename": "p.png", "data": "QUJDREVG"},
            {"type": "text", "mediaType": "text/plain", "data": "c2VjcmV0"},
        ]
    }
    out = redact(payload)
    for item in out["attachments"]:
        assert item["data"] == REDACTED
    # Metadata survives for diagnostics.
    assert out["attachments"][0]["type"] == "image"
    assert out["attachments"][0]["mediaType"] == "image/png"
    assert out["attachments"][0]["filename"] == "p.png"


def test_redact_attachments_nested_in_request_body() -> None:
    body = {
        "userId": "u",
        "message": "hi",
        "attachments": [{"type": "image", "mediaType": "image/png", "data": "BASE64DATA"}],
    }
    out = redact(body)
    assert out["attachments"][0]["data"] == REDACTED
    assert "BASE64DATA" not in str(out)


def test_redact_attachments_non_list_passthrough() -> None:
    # Defensive: a non-list "attachments" value must not crash; falls back to recursive redact.
    out = redact({"attachments": {"data": "x", "token": "t"}})
    # token is redacted by the generic denylist; structure preserved.
    assert out["attachments"]["token"] == REDACTED


def test_redact_does_not_mutate_original() -> None:
    src = {"attachments": [{"data": "keepme"}]}
    redact(src)
    assert src["attachments"][0]["data"] == "keepme"


# ----------------------------- scenario 9 / §40: schema guards -----------------------------
def test_attachment_rejects_extra_fields() -> None:
    # StrictModel => extra='forbid'. A url source field is not accepted (anti-SSRF, only base64).
    with pytest.raises(ValidationError):
        AttachmentIn(
            type="image",
            mediaType="image/png",
            data="AAAA",
            source="https://evil.example/x.png",  # type: ignore[call-arg]
        )


def test_attachment_rejects_url_source_type() -> None:
    with pytest.raises(ValidationError):
        AttachmentIn(
            type="image",
            mediaType="image/png",
            data="AAAA",
            url="https://evil.example/x.png",  # type: ignore[call-arg]
        )


def test_attachment_requires_non_empty_data() -> None:
    with pytest.raises(ValidationError):
        AttachmentIn(type="image", mediaType="image/png", data="")


def test_tool_result_rejects_attachments_extra_forbid() -> None:
    import uuid

    with pytest.raises(ValidationError):
        ChatToolResultRequest(
            userId=uuid.uuid4(),
            sessionId=uuid.uuid4(),
            toolCallId=uuid.uuid4(),
            result={"ok": True},
            attachments=[  # type: ignore[call-arg]
                {"type": "image", "mediaType": "image/png", "data": "AAAA"}
            ],
        )
