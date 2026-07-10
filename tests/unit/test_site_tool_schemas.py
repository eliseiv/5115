"""Unit: server-side site.* tool schemas + name mapping (ADR-011, website-builder/09-testing.md).

Strict Pydantic (extra='forbid'); invalid encoding rejected; path traversal rejected; the
domain↔anthropic name map is bidirectional for site.* names.
"""

from __future__ import annotations

import pytest

from app.chat.tools import (
    SERVER_SIDE_TOOLS,
    TOOL_SITE_DELETE,
    TOOL_SITE_WRITE_FILE,
    UnknownToolNameError,
    to_anthropic_tool_name,
    to_domain_tool_name,
    validate_tool_args,
)


def test_site_write_file_ok() -> None:
    out = validate_tool_args(
        TOOL_SITE_WRITE_FILE,
        {
            "path": "index.html",
            "content": "PGgxPg==",
            "contentType": "text/html",
            "encoding": "base64",
        },
    )
    assert out["path"] == "index.html"
    assert out["encoding"] == "base64"
    assert out["contentType"] == "text/html"


def test_site_write_file_rejects_bad_encoding() -> None:
    with pytest.raises(ValueError):
        validate_tool_args(
            TOOL_SITE_WRITE_FILE,
            {"path": "i.html", "content": "x", "contentType": "text/html", "encoding": "hex"},
        )


def test_site_write_file_rejects_extra_field() -> None:
    with pytest.raises(ValueError):
        validate_tool_args(
            TOOL_SITE_WRITE_FILE,
            {
                "path": "i.html",
                "content": "x",
                "contentType": "text/html",
                "encoding": "utf8",
                "userId": "attacker",  # IDOR: ownership must NOT be model-supplied
            },
        )


def test_site_write_file_rejects_path_traversal() -> None:
    with pytest.raises(ValueError, match="traversal"):
        validate_tool_args(
            TOOL_SITE_WRITE_FILE,
            {
                "path": "../escape.html",
                "content": "x",
                "contentType": "text/html",
                "encoding": "utf8",
            },
        )


def test_site_delete_rejects_extra_project_field() -> None:
    # site.delete args carry ONLY path — no projectId/userId (session context owns those).
    with pytest.raises(ValueError):
        validate_tool_args(TOOL_SITE_DELETE, {"path": "a.html", "projectId": "other"})


def test_site_names_map_bidirectional() -> None:
    for domain in SERVER_SIDE_TOOLS:
        wire = to_anthropic_tool_name(domain)
        assert "." not in wire
        assert to_domain_tool_name(wire) == domain


def test_unknown_site_wire_name_raises() -> None:
    with pytest.raises(UnknownToolNameError):
        to_domain_tool_name("site_unknown_tool")
