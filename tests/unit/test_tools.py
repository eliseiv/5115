"""Unit tests for tool-arg validation and definitions (CO-1)."""

from __future__ import annotations

import pytest

from app.chat.tools import (
    ALL_TOOL_NAMES,
    MUTATING_TOOLS,
    TOOL_QUIZ_GENERATE,
    anthropic_tool_definitions,
    to_anthropic_tool_name,
    to_domain_tool_name,
    validate_tool_args,
)

# ADR-057 §4: quiz.generate is offered to the model ONLY under dialog_mode="study_learn"; every
# other definitions call omits it. The non-quiz set is the default offered catalog.
_NON_QUIZ_TOOLS = set(ALL_TOOL_NAMES) - {TOOL_QUIZ_GENERATE}


def test_validate_files_write_ok() -> None:
    out = validate_tool_args(
        "files.write",
        {"path": "a/b.txt", "content": "x", "encoding": "utf8", "overwrite": True},
    )
    assert out["path"] == "a/b.txt"
    assert out["encoding"] == "utf8"


def test_validate_rejects_path_traversal() -> None:
    with pytest.raises(ValueError, match="traversal"):
        validate_tool_args(
            "files.write",
            {"path": "../etc/passwd", "content": "x", "encoding": "utf8", "overwrite": False},
        )


def test_validate_rejects_backslash_traversal() -> None:
    with pytest.raises(ValueError, match="traversal"):
        validate_tool_args("files.read", {"path": "a\\..\\b"})


def test_validate_rejects_unknown_tool() -> None:
    with pytest.raises(ValueError, match="unknown tool"):
        validate_tool_args("files.delete", {"path": "x"})


def test_validate_rejects_extra_fields() -> None:
    with pytest.raises(ValueError):
        validate_tool_args("files.read", {"path": "x", "unexpected": 1})


def test_validate_rejects_missing_required() -> None:
    with pytest.raises(ValueError):
        validate_tool_args("files.write", {"path": "x"})


def test_calendar_create_nested_events() -> None:
    out = validate_tool_args(
        "calendar.create_events",
        {"events": [{"title": "t", "start": "2026-01-01", "end": "2026-01-02"}]},
    )
    assert len(out["events"]) == 1


def test_mutating_tools_subset() -> None:
    assert MUTATING_TOOLS <= ALL_TOOL_NAMES
    assert "files.write" in MUTATING_TOOLS
    assert "files.read" not in MUTATING_TOOLS


def test_anthropic_definitions_cover_all_non_quiz_tools() -> None:
    # BUG-3: definitions sent to Anthropic carry the WIRE (underscore) names, NOT the dotted
    # domain names. Anthropic rejects a dot in tool.name with 400 → backend 502. The domain
    # contract (toolCall.name, DB, audit) stays dotted; only the transport boundary maps.
    # ADR-057 §4: with the default dialog_mode (None ≠ study_learn) quiz.generate is NOT offered, so
    # the definitions cover exactly the non-quiz set.
    defs = anthropic_tool_definitions()
    names = {d["name"] for d in defs}
    # The emitted names are the underscore wire names (no dots), one per offered domain tool.
    assert names == {to_anthropic_tool_name(n) for n in _NON_QUIZ_TOOLS}
    assert all("." not in n for n in names)
    # Each wire name reverse-maps back to exactly the offered domain tool set (bijective, lossless).
    assert {to_domain_tool_name(n) for n in names} == _NON_QUIZ_TOOLS
    assert "quiz_generate" not in names
    for d in defs:
        assert "input_schema" in d
        assert d["description"]


def test_anthropic_definitions_add_quiz_under_study_learn() -> None:
    # ADR-057 §4: under study_learn the offered set covers EVERY registered tool + quiz.generate.
    defs = anthropic_tool_definitions(dialog_mode="study_learn")
    names = {d["name"] for d in defs}
    assert names == {to_anthropic_tool_name(n) for n in ALL_TOOL_NAMES}
    assert "quiz_generate" in names
    assert {to_domain_tool_name(n) for n in names} == set(ALL_TOOL_NAMES)
