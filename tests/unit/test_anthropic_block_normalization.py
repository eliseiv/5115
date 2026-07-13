"""Unit tests for ADR-021 content-block normalization (_normalize_block, scenario 3).

block.model_dump() from the Anthropic SDK carries non-wire fields (e.g. "caller":{"type":"direct"})
that are garbage on replay and break the payload-purity invariant. _normalize_block keeps ONLY the
wire-valid fields per block type (allowlist), preserving raw tool_use.id verbatim (ADR-008) and
never losing real content. Unknown block types must not blow up (forward-compatible).

These are pure-function tests (no I/O); the persist-boundary integration (payload + assembled
messages carry no `caller`) is covered in tests/integration/test_chat_tool_loop_seq.py.
"""

from __future__ import annotations

from app.chat.anthropic_client import _BLOCK_WIRE_FIELDS, _normalize_block


def test_tool_use_strips_caller_keeps_wire_fields_and_raw_id() -> None:
    # ADR-021 root of problem 2: SDK adds `caller` to a tool_use block. ADR-008: raw id verbatim.
    raw = {
        "type": "tool_use",
        "id": "toolu_01ABCdef234567890XYZ",
        "name": "site_write_file",
        "input": {"path": "index.html", "content": "<h1>hi</h1>"},
        "caller": {"type": "direct"},
    }
    out = _normalize_block(raw)
    assert "caller" not in out
    assert out == {
        "type": "tool_use",
        "id": "toolu_01ABCdef234567890XYZ",  # raw provider id preserved verbatim (ADR-008)
        "name": "site_write_file",
        "input": {"path": "index.html", "content": "<h1>hi</h1>"},
    }


def test_tool_use_drops_any_future_non_wire_field_via_allowlist() -> None:
    # Allowlist (not point-removal of `caller`): any unknown SDK annotation is dropped too.
    raw = {
        "type": "tool_use",
        "id": "toolu_x",
        "name": "example_client_tool",
        "input": {"path": "a"},
        "caller": {"type": "direct"},
        "some_future_sdk_field": {"nested": 1},
        "cache_control": {"type": "ephemeral"},
    }
    out = _normalize_block(raw)
    assert set(out.keys()) == {"type", "id", "name", "input"}


def test_text_block_keeps_only_type_and_text() -> None:
    raw = {"type": "text", "text": "hello world", "citations": None, "caller": {"type": "direct"}}
    assert _normalize_block(raw) == {"type": "text", "text": "hello world"}


def test_image_block_keeps_type_and_source() -> None:
    source = {"type": "base64", "media_type": "image/png", "data": "QUJD"}
    raw = {"type": "image", "source": source, "caller": {"type": "direct"}}
    assert _normalize_block(raw) == {"type": "image", "source": source}


def test_document_block_keeps_type_and_source() -> None:
    source = {"type": "base64", "media_type": "application/pdf", "data": "JVBE"}
    raw = {"type": "document", "source": source, "title": "x", "caller": {"type": "direct"}}
    assert _normalize_block(raw) == {"type": "document", "source": source}


def test_thinking_block_keeps_wire_fields() -> None:
    raw = {
        "type": "thinking",
        "thinking": "let me think",
        "signature": "sig123",
        "caller": {"type": "direct"},
    }
    assert _normalize_block(raw) == {
        "type": "thinking",
        "thinking": "let me think",
        "signature": "sig123",
    }


def test_redacted_thinking_block_keeps_data() -> None:
    raw = {"type": "redacted_thinking", "data": "encrypted-blob", "caller": {"type": "direct"}}
    assert _normalize_block(raw) == {"type": "redacted_thinking", "data": "encrypted-blob"}


def test_unknown_block_type_does_not_raise_and_only_drops_caller() -> None:
    # Forward-compat: an unknown future block type keeps all content, dropping only `caller`.
    raw = {"type": "future_block_xyz", "payload": {"k": "v"}, "caller": {"type": "direct"}}
    out = _normalize_block(raw)
    assert out == {"type": "future_block_xyz", "payload": {"k": "v"}}


def test_block_missing_type_does_not_raise() -> None:
    # Defensive: a block without a "type" key must not crash; treated as unknown → drop caller only.
    raw = {"caller": {"type": "direct"}, "stuff": 1}
    assert _normalize_block(raw) == {"stuff": 1}


def test_allowlist_keeps_only_present_fields_no_kerror() -> None:
    # A wire field absent in the block must not be invented (comprehension is membership-gated).
    raw = {"type": "tool_use", "id": "toolu_y", "name": "example_client_tool"}  # no "input"
    out = _normalize_block(raw)
    assert out == {"type": "tool_use", "id": "toolu_y", "name": "example_client_tool"}
    assert "input" not in out


def test_wire_allowlist_table_covers_documented_block_types() -> None:
    # ADR-021 Decision §2 enumerates these block types; guard against accidental allowlist drift.
    assert set(_BLOCK_WIRE_FIELDS) == {
        "text",
        "image",
        "document",
        "tool_use",
        "thinking",
        "redacted_thinking",
    }
    assert _BLOCK_WIRE_FIELDS["tool_use"] == ("type", "id", "name", "input")
