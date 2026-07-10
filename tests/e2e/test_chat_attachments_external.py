"""E2E against the REAL Anthropic API: multimodal message (image + PDF + text) (ADR-020, TD-016).

Purpose: confirm wire-compatibility of the inline image / native document(PDF) / text blocks on
anthropic SDK 0.39.0 against a live endpoint (TD-016 — the SDK has no DocumentBlockParam, so the
document block is sent as a raw dict and only a live call proves the wire format is accepted).

STATUS: BLOCKED by an EXTERNAL account issue, NOT by the code. The configured production Anthropic
key belongs to a DISABLED organization, so the live API returns
`400: "This organization has been disabled."` regardless of our payload. This test is therefore
SKIPPED in CI and on local runs; it is kept as the executable spec for when a working key is
available. The document-block WIRE SHAPE is verified at the unit level
(tests/unit/test_attachments.py::test_pdf_attachment_maps_to_document_dict_block), independent of a
live call. Marked @pytest.mark.external so it can be selected explicitly (`-m external`) once the
account is re-enabled.
"""

from __future__ import annotations

import base64
import io
import os

import pytest

_SKIP_REASON = (
    "Blocked by external account: the production Anthropic key belongs to a DISABLED organization "
    "(400 'This organization has been disabled.'). Not a code failure (ADR-020, TD-016). Re-enable "
    "the org or supply a working ANTHROPIC_API_KEY_E2E, then run with `-m external`."
)


def _png_b64() -> str:
    return base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64).decode("ascii")


def _pdf_b64() -> str:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    buf = io.BytesIO()
    writer.write(buf)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _text_b64() -> str:
    return base64.b64encode(b"line one\nline two\n").decode("ascii")


@pytest.mark.external
@pytest.mark.skip(reason=_SKIP_REASON)
@pytest.mark.asyncio
async def test_real_anthropic_image_pdf_text_in_one_message() -> None:
    """Send image + PDF + text in a single user message to the REAL API and expect a text reply.

    Confirms the document block (raw wire dict, TD-016) is accepted alongside image/text by the
    live Messages API for the configured model. Skipped: the configured org is disabled.
    """
    from app.chat.anthropic_client import AnthropicClient
    from app.chat.attachments import prepare_attachments
    from app.config import get_settings
    from app.schemas.chat import AttachmentIn

    api_key = os.environ.get("ANTHROPIC_API_KEY_E2E") or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        pytest.skip("no ANTHROPIC_API_KEY for the external e2e")

    settings = get_settings()
    attachments = [
        AttachmentIn(type="image", mediaType="image/png", filename="p.png", data=_png_b64()),
        AttachmentIn(
            type="document", mediaType="application/pdf", filename="d.pdf", data=_pdf_b64()
        ),
        AttachmentIn(type="text", mediaType="text/plain", filename="n.txt", data=_text_b64()),
    ]
    prepared = prepare_attachments(attachments, settings)

    user_content = [
        {"type": "text", "text": "Describe the attachments briefly."},
        *prepared.content_blocks,
    ]
    messages = [{"role": "user", "content": user_content}]

    client = AnthropicClient()
    result = await client.create_message(
        system_prompt="You are a helpful assistant.",
        messages=messages,
        tools=[],
        api_key=api_key,
    )
    # A successful live call returns a final assistant text turn.
    assert result.stop_reason in {"end_turn", "max_tokens"}
    assert isinstance(result.text, str)
    assert result.text != ""
