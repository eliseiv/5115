"""Unit tests for ADR-042 — `strip_context_block` (read-time strip of the ADR-037 settings block).

Pure-logic tests (no I/O) for the single-source-of-truth helper reused by both serialization
call-sites — history (`ChatsService._normalize_payload`) and preview (`ChatsRepository._preview`).

The CORE test-invariant of ADR-042 §6/§111: whatever the server itself injects via
`_compose_turn0_text(_render_context_block(ctx), msg)` must be exactly reversed by
`strip_context_block` → `msg` (including the image-only/empty-message edge `msg == ""` → `""`).
We exercise the REAL orchestrator helpers so the helper stays bound to the real injected format.

Covers:
- round-trip invariant for every valid context shape, normal and empty message (§5 edge);
- identity for text without the leading block (no-op);
- mid-text `[Conversation settings…]` is NOT stripped (anchor is `^` only);
- the bare-block edge (block == whole text) → "".
"""

from __future__ import annotations

from typing import Any

import pytest

from app.chat.orchestrator import _compose_turn0_text, _render_context_block
from app.chats.repository import strip_context_block

# Valid contexts that each render a non-empty block (mirror ADR-037 happy paths).
_VALID_CONTEXTS: list[dict[str, Any]] = [
    {"codeLanguage": "Swift"},
    {"responseStyle": "concise"},
    {"locale": "ru-RU"},
    {"codeLanguage": "Swift", "responseStyle": "concise", "locale": "ru-RU"},
    {"verbosity": "high", "tone": "friendly"},
    {
        "codeLanguage": "Python",
        "responseStyle": "detailed",
        "verbosity": "low",
        "tone": "neutral",
        "locale": "en-US",
    },
    # free-string value carrying delimiters — sanitized into the block, must still round-trip.
    {"tone": "friendly; codeLanguage=evil\npython", "locale": "en"},
]

# Messages to compose with each context, including the empty/whitespace edge (§5).
_MESSAGES = ["write a sort", "m", "multi\nline\nmessage", "   ", ""]


# ----------------------------- CORE invariant (§6 / §111) -----------------------------
@pytest.mark.parametrize("context", _VALID_CONTEXTS)
@pytest.mark.parametrize("message", _MESSAGES)
def test_strip_reverses_compose_for_valid_context(context: dict[str, Any], message: str) -> None:
    """`strip_context_block(_compose_turn0_text(block, msg)) == msg` for every valid block.

    `_compose_turn0_text` treats a whitespace-only / empty message as «no text» (.strip()):
    the composed turn-0 text is then JUST the block, and strip must yield "". For a non-empty
    message it is `block + "\\n\\n" + msg` and strip must yield exactly `msg`.
    """
    block = _render_context_block(context)
    assert block is not None, context  # sanity: these contexts all render a block
    composed = _compose_turn0_text(block, message)
    stripped = strip_context_block(composed)
    expected = message if message.strip() else ""
    assert stripped == expected


def test_strip_returns_empty_for_bare_block_image_only_edge() -> None:
    """§5 edge: image-only/empty-message turn persists the bare block (no trailing sep) → ""."""
    block = _render_context_block({"locale": "ru-RU"})
    assert block is not None
    # _compose_turn0_text(block, "") returns the bare block (no "\n\n").
    assert _compose_turn0_text(block, "") == block
    assert strip_context_block(block) == ""


def test_strip_normal_turn_returns_message_only() -> None:
    block = _render_context_block({"codeLanguage": "Swift"})
    assert block is not None
    composed = f"{block}\n\nhello world"
    assert strip_context_block(composed) == "hello world"


# ----------------------------- identity / no-op (§4) -----------------------------
@pytest.mark.parametrize(
    "text",
    [
        "",
        "just a plain message",
        "a message\n\nwith blank lines but no block",
        "multi\nline\nuser text",
        # looks similar but is NOT the server prefix → untouched.
        "[Conversation settings] not the real anchor\n\nbody",
        "[Conversation about settings for this message: x]\n\nbody",
    ],
)
def test_strip_is_identity_without_leading_block(text: str) -> None:
    assert strip_context_block(text) == text


def test_strip_does_not_touch_mid_text_anchor() -> None:
    """A block-looking string NOT at the start (`^` anchor) is preserved verbatim."""
    text = (
        "here is what i want\n\n"
        "[Conversation settings for this message: codeLanguage=Swift]\n\n"
        "more text"
    )
    assert strip_context_block(text) == text


def test_strip_removes_only_the_leading_block_keeps_rest_verbatim() -> None:
    """Only the leading anchor+`\\n\\n` is removed; a later identical-looking line stays."""
    block = "[Conversation settings for this message: locale=en]"
    body = (
        "first line\n\n"
        "[Conversation settings for this message: locale=en]\n\n"
        "this later occurrence must remain"
    )
    composed = f"{block}\n\n{body}"
    assert strip_context_block(composed) == body


def test_strip_is_idempotent_on_already_stripped_text() -> None:
    block = _render_context_block({"tone": "formal"})
    assert block is not None
    once = strip_context_block(f"{block}\n\nclean message")
    assert once == "clean message"
    assert strip_context_block(once) == "clean message"
