"""Unit tests for the GET /v1/tools catalog payload (ADR-019, chat-orchestrator/02).

``tool_catalog()`` is the single source of truth backing the endpoint; these tests assert the
catalog contract (16 tools — ADR-026 added the global server-side ``time.now``, ADR-057 added
``quiz.generate``, ADR-058 added ``image.generate`` — dotted domain names, correct
mutating/execution flags, inputSchema) without an app/DB round-trip. The HTTP wiring
(JWT-protection, response shape) is exercised in tests/integration/test_tools_endpoint.py.

ADR-058 §3 note: the ``image.generate`` KEY-GATE (offered to the LLM only when OPENAI_API_KEY is
set) constrains only the OFFERED tool-set — the machine-readable ``tool_catalog()`` lists EVERY
registered tool UNCONDITIONALLY (like the dialog-mode gate for quiz.generate). So image.generate is
always present here regardless of the key.
"""

from __future__ import annotations

from app.chat.tools import (
    ALL_TOOL_NAMES,
    GLOBAL_SERVER_SIDE_TOOLS,
    MUTATING_TOOLS,
    SERVER_SIDE_TOOLS,
    tool_catalog,
)

# Per ADR-011 / ADR-026 / ADR-057 / ADR-058 / chat-orchestrator/02: 8 client-side iOS tools + 5
# server-side site.* tools + 3 global server-side tools (time.now, quiz.generate, image.generate)
# = 16.
_EXPECTED_NAMES = {
    "files.read",
    "files.write",
    "files.list",
    "files.mkdir",
    "calendar.read",
    "calendar.create_events",
    "reminders.read",
    "reminders.create",
    "site.write_file",
    "site.preview",
    "site.list",
    "site.read",
    "site.delete",
    "time.now",
    "quiz.generate",
    "image.generate",
}


def test_catalog_lists_every_registered_tool() -> None:
    catalog = tool_catalog()
    assert len(catalog) == 16
    assert {t["name"] for t in catalog} == _EXPECTED_NAMES == set(ALL_TOOL_NAMES)


def test_quiz_generate_is_global_server_side_in_catalog() -> None:
    # ADR-057: quiz.generate is a GLOBAL server-side tool → execution="server", non-mutating. The
    # /v1/tools catalog lists it unconditionally (the study_learn dialog gate constrains only the
    # LLM offer-set, not this machine-readable catalog).
    assert "quiz.generate" in GLOBAL_SERVER_SIDE_TOOLS
    by_name = {t["name"]: t for t in tool_catalog()}
    entry = by_name["quiz.generate"]
    assert entry["execution"] == "server"
    assert entry["mutating"] is False


def test_every_tool_name_is_dotted_domain_not_underscore() -> None:
    # The iOS-facing contract uses dotted domain names (files.read, site.write_file); the
    # underscore wire names are an Anthropic-transport detail and must NOT leak here (BUG-3).
    for tool in tool_catalog():
        assert "." in tool["name"], tool["name"]
        assert "_" not in tool["name"].split(".")[0]  # the domain segment has no underscore


def test_mutating_flag_matches_mutating_tools() -> None:
    expected_mutating = {
        "files.write",
        "files.mkdir",
        "calendar.create_events",
        "reminders.create",
        "site.write_file",
        "site.delete",
        # ADR-058 §1/§4: image.generate WRITES bytes (generated_images) and is TARIFFED → mutating,
        # unlike the echo-only time.now/quiz.generate.
        "image.generate",
    }
    assert expected_mutating == set(MUTATING_TOOLS)
    by_name = {t["name"]: t for t in tool_catalog()}
    for name, tool in by_name.items():
        assert isinstance(tool["mutating"], bool)
        assert tool["mutating"] is (name in expected_mutating), name


def test_execution_is_server_for_site_and_global_and_client_otherwise() -> None:
    # ADR-026 §2: execution == "server" for project-scoped site.* AND global server-side time.now;
    # everything else is client-side.
    by_name = {t["name"]: t for t in tool_catalog()}
    for name, tool in by_name.items():
        expected = (
            "server" if name in SERVER_SIDE_TOOLS or name in GLOBAL_SERVER_SIDE_TOOLS else "client"
        )
        assert tool["execution"] == expected, (name, tool["execution"])
    # Cross-check: site.* and time.now are the server-side set.
    assert by_name["time.now"]["execution"] == "server"
    assert by_name["time.now"]["mutating"] is False
    for name in SERVER_SIDE_TOOLS:
        assert by_name[name]["execution"] == "server"


def test_every_tool_has_input_schema_and_description() -> None:
    for tool in tool_catalog():
        assert isinstance(tool["inputSchema"], dict)
        assert tool["inputSchema"], f"{tool['name']} has empty inputSchema"
        # JSON Schema object shape (Pydantic emits type=object with properties for arg models).
        assert tool["inputSchema"].get("type") == "object"
        assert isinstance(tool["description"], str) and tool["description"]
