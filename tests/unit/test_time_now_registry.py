"""Unit tests for ``time.now`` registry/offer wiring + system-prompt invariant (ADR-026 §2/§3/§7).

Asserts the global-server-side registration (``GLOBAL_SERVER_SIDE_TOOLS``), the offer-set rule
(``time.now`` offered with OR without a project; ``site.*`` gated by project), and that the static
date-free time.now instruction is present in both system prompts and never interpolates a date
(prompt-cache invariant).
"""

from __future__ import annotations

from app.chat.orchestrator import (
    _SYSTEM_PROMPT_CHAT,
    _SYSTEM_PROMPT_CODE,
    _TIME_NOW_INSTRUCTION,
    _system_prompt_for,
)
from app.chat.tools import (
    ALL_TOOL_NAMES,
    GLOBAL_SERVER_SIDE_TOOLS,
    MUTATING_TOOLS,
    SERVER_SIDE_TOOLS,
    TOOL_TIME_NOW,
    anthropic_tool_definitions,
    to_anthropic_tool_name,
    to_domain_tool_name,
)


def test_global_and_project_scoped_registries_are_disjoint() -> None:
    # ADR-026 §2 invariant: the two server-side registries are mutually exclusive.
    assert GLOBAL_SERVER_SIDE_TOOLS.isdisjoint(SERVER_SIDE_TOOLS)
    assert TOOL_TIME_NOW in GLOBAL_SERVER_SIDE_TOOLS
    assert TOOL_TIME_NOW in ALL_TOOL_NAMES
    # Read-only: time.now must NOT be a mutating tool (no tool_mutation audit).
    assert TOOL_TIME_NOW not in MUTATING_TOOLS


def test_time_now_name_maps_dot_to_underscore() -> None:
    assert to_anthropic_tool_name(TOOL_TIME_NOW) == "time_now"
    assert to_domain_tool_name("time_now") == TOOL_TIME_NOW


def test_offer_set_without_project_includes_time_now_excludes_site() -> None:
    # include_server_side=False == «чистый чат» (no project): site.* dropped, time.now kept.
    defs = anthropic_tool_definitions(include_server_side=False)
    names = {to_domain_tool_name(d["name"]) for d in defs}
    assert TOOL_TIME_NOW in names
    assert names.isdisjoint(SERVER_SIDE_TOOLS)
    # client-side tools remain offered; total = 8 client-side + time.now + image.generate (ADR-058:
    # the key-gate is satisfied — conftest forces a non-empty OPENAI_API_KEY — so the GLOBAL
    # server-side image.generate stays; only project-scoped site.* are dropped).
    assert "files.read" in names
    assert "image.generate" in names
    assert len(defs) == 10


def test_offer_set_with_project_includes_both_time_now_and_site() -> None:
    defs = anthropic_tool_definitions(include_server_side=True)
    names = {to_domain_tool_name(d["name"]) for d in defs}
    assert TOOL_TIME_NOW in names
    assert names >= SERVER_SIDE_TOOLS
    # 8 client-side + 5 site.* + time.now + image.generate (quiz.generate is dialog-gated out).
    assert len(defs) == 15


def test_time_now_definition_carries_description_and_schema() -> None:
    defs = anthropic_tool_definitions(include_server_side=False)
    time_def = next(d for d in defs if d["name"] == "time_now")
    assert time_def["description"]
    schema = time_def["input_schema"]
    assert schema["type"] == "object"
    # tz is the only property and it is optional (additionalProperties forbidden by _StrictModel).
    assert set(schema.get("properties", {})) == {"tz"}
    assert schema.get("additionalProperties") is False


# ----------------------------- system prompt invariant (ADR-026 §7) -----------------------------
def test_both_system_prompts_contain_time_now_instruction() -> None:
    assert _TIME_NOW_INSTRUCTION in _SYSTEM_PROMPT_CHAT
    assert _TIME_NOW_INSTRUCTION in _SYSTEM_PROMPT_CODE
    assert _TIME_NOW_INSTRUCTION in _system_prompt_for("chat")
    assert _TIME_NOW_INSTRUCTION in _system_prompt_for("code")
    # The instruction tells the model to call the tool and not guess.
    assert "time.now" in _TIME_NOW_INSTRUCTION
    assert "do not guess" in _TIME_NOW_INSTRUCTION


def test_system_prompt_is_static_no_date_interpolated() -> None:
    # prompt-cache invariant: the instruction is the SAME object every call (no date injected),
    # so the cached system prefix never changes between requests.
    assert _system_prompt_for("chat") == _SYSTEM_PROMPT_CHAT
    assert _system_prompt_for("chat") == _system_prompt_for("chat")
    # No 4-digit year is hardcoded into the static instruction (the date arrives via tool_result).
    import re

    assert re.search(r"\b20\d\d\b", _TIME_NOW_INSTRUCTION) is None
